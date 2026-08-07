import { expect, test, type Page, type Route } from "@playwright/test";

const csrf = "csrf_browser.secret-value-that-is-long-enough";
const expires = "2026-08-07T00:15:00Z";
const session = { csrf_token: csrf, expires_at: expires, scopes: ["runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"] };
const attack = `<img src=x onerror="window.__locus_xss=true">`;

async function fulfillJSON(route: Route, body: unknown, status = 200, headers: Record<string, string> = {}) {
  await route.fulfill({ status, contentType: "application/json", headers, body: JSON.stringify(body) });
}

async function commonRoutes(page: Page, authenticated = true) {
  await page.route("**/v1/browser/session", async (route) => {
    if (route.request().method() === "GET") {
      await fulfillJSON(route, authenticated ? session : { error: { code: "unauthenticated" } }, authenticated ? 200 : 401);
      return;
    }
    await route.continue();
  });
  await page.route("**/v1/runs", (route) => fulfillJSON(route, { runs: [] }));
  await page.route("**/v1/audit?limit=100", (route) => fulfillJSON(route, { records: [{ id: 9, aggregate_type: "job", aggregate_id: "11111111-1111-4111-8111-111111111111", event_type: attack, actor_type: "worker", created_at: "2026-08-06T23:00:00Z" }] }));
}

test("exchanges a token without browser storage and runs under the production CSP", async ({ page, context }) => {
  await commonRoutes(page, false);
  await page.route("**/v1/browser/sessions", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ token: "locus_durable-secret" });
    await fulfillJSON(route, session, 201, {
      "Set-Cookie": "__Host-locus_session=session_secret; Path=/; Max-Age=900; Secure; HttpOnly; SameSite=Strict",
    });
  });

  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("object-src 'none'");
  await page.getByRole("textbox", { name: "Workspace token" }).fill("locus_durable-secret");
  await page.getByRole("button", { name: "Open control room" }).click();
  await expect(page.getByText("Run inventory")).toBeVisible();
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  expect(await page.locator("img[onerror], script:not([type=module])").count()).toBe(0);
  expect(await page.evaluate(() => (window as Window & { __locus_xss?: boolean }).__locus_xss)).not.toBe(true);
  const cookies = await context.cookies();
  const cookie = cookies.find((item) => item.name === "__Host-locus_session");
  expect(cookie).toMatchObject({ httpOnly: true, secure: true, sameSite: "Strict" });
});

test("refresh reconstructs attempts and cancellation displays the database winner", async ({ page }) => {
  await commonRoutes(page);
  const jobID = "11111111-1111-4111-8111-111111111111";
  const running = {
    job_id: jobID, operation: "diff", state: "running", run_ids: ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"], profile: "lexical-v1", current_attempt_number: 2,
    attempts: [
      { attempt_number: 1, state: "fenced", started_at: "2026-08-06T23:00:00Z", finished_at: "2026-08-06T23:01:00Z", failure: { schema_version: 1, code: "lease_lost", message: "lease expired", retryable: true } },
      { attempt_number: 2, state: "running", started_at: "2026-08-06T23:02:00Z", finished_at: null, failure: null },
    ],
    progress: { protocol_version: 1, attempt_number: 2, sequence: 2, stage: "analyzing", message: "Aligning trajectories" }, cancel_requested_at: null, failure: null,
    created_at: "2026-08-06T23:00:00Z", updated_at: "2026-08-06T23:02:00Z", terminal_at: null, artifacts: [],
  };
  const succeeded = { ...running, state: "succeeded", cancel_requested_at: "2026-08-06T23:03:00Z", terminal_at: "2026-08-06T23:03:00Z", updated_at: "2026-08-06T23:03:00Z", attempts: [running.attempts[0], { ...running.attempts[1], state: "succeeded", finished_at: "2026-08-06T23:03:00Z" }] };
  let cancelled = false;
  let reads = 0;
  await page.route(`**/v1/jobs/${jobID}`, async (route) => { reads += 1; await fulfillJSON(route, cancelled ? succeeded : running); });
  await page.route(`**/v1/jobs/${jobID}/cancel`, async (route) => {
    expect(route.request().headers()["x-locus-csrf"]).toBe(csrf);
    cancelled = true;
    await fulfillJSON(route, succeeded);
  });
  await page.route(`**/v1/jobs/${jobID}/events`, (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));

  await page.goto(`/jobs/${jobID}`);
  await expect(page.getByText("Aligning trajectories")).toBeVisible();
  await expect(page.getByText("fenced")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Attempt 2")).toBeVisible();
  expect(reads).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: "Request cancellation" }).click();
  await expect(page.getByText("Terminal state committed")).toBeVisible();
  await expect(page.getByText(/Cancellation requested/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Request cancellation" })).toHaveCount(0);
});
