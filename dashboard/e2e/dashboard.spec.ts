import { expect, test, type Page, type Route } from "@playwright/test";

const csrf = "csrf_browser.secret-value-that-is-long-enough";
const expires = "2026-08-07T00:15:00Z";
const session = { csrf_token: csrf, expires_at: expires, scopes: ["runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"] };
const attack = `<img src=x onerror="window.__tracewake_xss=true">`;

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
    expect(route.request().postDataJSON()).toEqual({ token: "tracewake_durable-secret" });
    await fulfillJSON(route, session, 201, {
      "Set-Cookie": "__Host-tracewake_session=session_secret; Path=/; Max-Age=900; Secure; HttpOnly; SameSite=Strict",
    });
  });

  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("object-src 'none'");
  await page.getByRole("textbox", { name: "Workspace token" }).fill("tracewake_durable-secret");
  await page.getByRole("button", { name: "Open control room" }).click();
  await expect(page.getByText("Run inventory")).toBeVisible();
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length }))).toEqual({ local: 0, session: 0 });
  expect(await page.locator("img[onerror], script:not([type=module])").count()).toBe(0);
  expect(await page.evaluate(() => (window as Window & { __tracewake_xss?: boolean }).__tracewake_xss)).not.toBe(true);
  const cookies = await context.cookies();
  const cookie = cookies.find((item) => item.name === "__Host-tracewake_session");
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
    expect(route.request().headers()["x-tracewake-csrf"]).toBe(csrf);
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

test("uploads through the same-origin control plane without a storage capability", async ({ page }) => {
  await commonRoutes(page);
  const runID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const seen: string[] = [];
  await page.route("**/v1/browser/runs/uploads", async (route) => {
    seen.push(route.request().url());
    expect(route.request().headers()["x-tracewake-csrf"]).toBe(csrf);
    expect(route.request().postDataJSON()).toMatchObject({ bundle_format_version: 1, bundle_size: 13 });
    await fulfillJSON(route, { run_id: runID, state: "pending" }, 201);
  });
  await page.route(`**/v1/browser/runs/uploads/${runID}`, async (route) => {
    seen.push(route.request().url());
    expect(route.request().method()).toBe("PUT");
    expect(route.request().headers()["x-tracewake-csrf"]).toBe(csrf);
    expect(route.request().headers()["x-tracewake-bundle-format"]).toBe("1");
    expect(route.request().postData()).toBe("bundle bytes\n");
    await fulfillJSON(route, { run_id: runID, state: "validating" });
  });
  await page.route(`**/v1/runs/${runID}`, (route) => fulfillJSON(route, {
    run_id: runID, state: "validating", bundle_digest: "a".repeat(64), bundle_format_version: 1,
    logical_run_digest: null, cassette_format_version: null, event_schema_version: null, event_count: null,
    failure: null, created_at: "2026-08-06T23:00:00Z", retention_expires_at: "2026-11-04T23:00:00Z", ready_at: null,
  }));

  await page.goto("/");
  await expect(page.getByText("Run inventory")).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles("e2e/fixtures/browser.bundle.tar");
  await expect(page.getByText(/Validation queued for/)).toBeVisible();
  expect(seen).toHaveLength(2);
  expect(seen.every((url) => new URL(url).origin === new URL(page.url()).origin)).toBe(true);
  expect(seen.some((url) => url.includes("amazonaws") || url.includes("s3"))).toBe(false);
});

test("runs the self-contained report renderer without same-origin authority", async ({ page }) => {
  await commonRoutes(page);
  const jobID = "22222222-2222-4222-8222-222222222222";
  const resultID = "33333333-3333-4333-8333-333333333333";
  const reportID = "44444444-4444-4444-8444-444444444444";
  await page.route(`**/v1/jobs/${jobID}`, (route) => fulfillJSON(route, {
    job_id: jobID, operation: "diff", state: "succeeded", run_ids: ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"], profile: "lexical-v1", current_attempt_number: 1,
    attempts: [{ attempt_number: 1, state: "succeeded", started_at: "2026-08-06T23:00:00Z", finished_at: "2026-08-06T23:01:00Z", failure: null }],
    progress: null, cancel_requested_at: null, failure: null, created_at: "2026-08-06T23:00:00Z", updated_at: "2026-08-06T23:01:00Z", terminal_at: "2026-08-06T23:01:00Z",
    artifacts: [
      { artifact_id: resultID, kind: "diff_json", digest: "a".repeat(64), size: 100, media_type: "application/json", schema_name: "result-envelope", schema_version: 1, retention_expires_at: "2026-11-06T23:00:00Z" },
      { artifact_id: reportID, kind: "diff_html", digest: "b".repeat(64), size: 100, media_type: "text/html; charset=utf-8", schema_name: null, schema_version: null, retention_expires_at: "2026-11-06T23:00:00Z" },
    ],
  }));
  await page.route(`**/v1/browser/artifacts/${resultID}`, (route) => fulfillJSON(route, {
    protocol_version: 1, status: "succeeded", failure: null, result: { kind: "diff", provenance: {} },
  }));
  await page.route(`**/v1/jobs/${jobID}/events`, (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.route(`**/v1/browser/artifacts/${reportID}?disposition=inline`, (route) => route.fulfill({
    status: 200,
    contentType: "text/html; charset=utf-8",
    headers: { "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'; sandbox allow-scripts; frame-ancestors 'self'" },
    body: "<!doctype html><body>waiting<script>document.body.textContent='renderer ran'</script></body>",
  }));

  await page.goto(`/jobs/${jobID}`);
  await page.getByRole("button", { name: "View safely" }).click();
  const frame = page.locator('iframe[title="Tracewake diff report"]');
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  await expect(page.frameLocator('iframe[title="Tracewake diff report"]').getByText("renderer ran")).toBeVisible();
});
