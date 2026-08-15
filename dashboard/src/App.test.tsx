import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Job, Run } from "./types";

const session = {
  csrf_token: "csrf_test.secret-value-that-is-long-enough",
  expires_at: "2026-08-07T00:15:00Z",
  scopes: ["runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"],
};

function run(id: string, created: string): Run {
  return {
    run_id: id,
    state: "ready",
    bundle_digest: "a".repeat(64),
    bundle_format_version: 1,
    logical_run_digest: "b".repeat(64),
    cassette_format_version: 1,
    event_schema_version: 3,
    event_count: 1,
    failure: null,
    created_at: created,
    retention_expires_at: "2026-11-06T23:00:00Z",
    ready_at: created,
  };
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

class FakeEventSource {
  addEventListener() {}
  close() {}
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("browser session exchange", () => {
  it("clears the durable token and never writes browser storage", async () => {
    const stored = vi.spyOn(Storage.prototype, "setItem");
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/v1/browser/session") return json({ error: { code: "unauthenticated" } }, 401);
      if (url === "/v1/browser/sessions") return json(session, 201);
      if (url === "/v1/runs") return json({ runs: [] });
      if (url.startsWith("/v1/audit")) return json({ records: [] });
      throw new Error(`unexpected request ${url}`);
    }));

    render(<App />);
    const token = await screen.findByLabelText("Workspace token") as HTMLInputElement;
    await userEvent.type(token, "tracewake_secret-that-must-not-persist");
    await userEvent.click(screen.getByRole("button", { name: "Open control room" }));
    await screen.findByText("Run inventory");

    expect(token.value).toBe("");
    expect(stored).not.toHaveBeenCalled();
    const exchange = calls.find((call) => call.url === "/v1/browser/sessions");
    expect(String(exchange?.init?.body)).toContain("tracewake_secret-that-must-not-persist");
    expect(calls.filter((call) => call.url !== "/v1/browser/sessions").every((call) => !String(call.init?.body).includes("tracewake_secret"))).toBe(true);
  });
});

describe("run inventory pagination", () => {
  it("loads cursor pages without presenting the first page as the full inventory", async () => {
    const first = run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "2026-08-06T23:01:00Z");
    const second = run("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "2026-08-06T23:00:00Z");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/v1/browser/session") return json(session);
      if (url === "/v1/runs") return json({ runs: [first], next_cursor: "second-page" });
      if (url === "/v1/runs?cursor=second-page") return json({ runs: [second], next_cursor: null });
      if (url.startsWith("/v1/audit")) return json({ records: [] });
      throw new Error(`unexpected request ${url}`);
    }));

    render(<App />);
    await screen.findByRole("checkbox", { name: `Compare run ${first.run_id}` });
    expect(screen.getByText("Runs loaded").previousElementSibling?.textContent).toBe("01");
    await userEvent.click(screen.getByRole("button", { name: "Load more runs" }));
    await screen.findByRole("checkbox", { name: `Compare run ${second.run_id}` });
    expect(screen.getByText("Runs loaded").previousElementSibling?.textContent).toBe("02");
    expect(screen.queryByRole("button", { name: "Load more runs" })).toBeNull();
  });
});

describe("authoritative job rendering", () => {
  it("renders provenance from the semantic result inside its envelope", async () => {
    const jobID = "11111111-1111-4111-8111-111111111111";
    window.history.replaceState({}, "", `/jobs/${jobID}`);
    const job: Job = {
      job_id: jobID,
      operation: "otlp",
      state: "succeeded",
      run_ids: ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
      profile: null,
      current_attempt_number: 1,
      attempts: [{ attempt_number: 1, state: "succeeded", started_at: "2026-08-06T23:00:00Z", finished_at: "2026-08-06T23:01:00Z", failure: null }],
      progress: null,
      cancel_requested_at: null,
      failure: null,
      created_at: "2026-08-06T23:00:00Z",
      updated_at: "2026-08-06T23:01:00Z",
      terminal_at: "2026-08-06T23:01:00Z",
      artifacts: [{ artifact_id: "result", kind: "otlp_result_json", digest: "a".repeat(64), size: 100, media_type: "application/json", schema_name: "result-envelope", schema_version: 1, retention_expires_at: "2026-11-06T23:00:00Z" }],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/v1/browser/session") return json(session);
      if (url === `/v1/jobs/${jobID}`) return json(job);
      if (url === "/v1/browser/artifacts/result") return json({
        protocol_version: 1,
        status: "succeeded",
        failure: null,
        result: { kind: "otlp", provenance: { analysis_profile: "otlp-spans-v1", worker_build: "worker-123" } },
      });
      throw new Error(`unexpected request ${url}`);
    }));

    render(<App />);
    await screen.findByText("otlp-spans-v1");
    expect(screen.getByText("worker-123")).not.toBeNull();
  });

  it("gives a self-contained HTML report scripts without same-origin authority", async () => {
    const jobID = "22222222-2222-4222-8222-222222222222";
    window.history.replaceState({}, "", `/jobs/${jobID}`);
    const job: Job = {
      job_id: jobID,
      operation: "diff",
      state: "succeeded",
      run_ids: ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"],
      profile: "align-v1",
      current_attempt_number: 1,
      attempts: [{ attempt_number: 1, state: "succeeded", started_at: "2026-08-06T23:00:00Z", finished_at: "2026-08-06T23:01:00Z", failure: null }],
      progress: null,
      cancel_requested_at: null,
      failure: null,
      created_at: "2026-08-06T23:00:00Z",
      updated_at: "2026-08-06T23:01:00Z",
      terminal_at: "2026-08-06T23:01:00Z",
      artifacts: [
        { artifact_id: "result", kind: "diff_json", digest: "a".repeat(64), size: 100, media_type: "application/json", schema_name: "result-envelope", schema_version: 1, retention_expires_at: "2026-11-06T23:00:00Z" },
        { artifact_id: "report", kind: "diff_html", digest: "b".repeat(64), size: 100, media_type: "text/html; charset=utf-8", schema_name: null, schema_version: null, retention_expires_at: "2026-11-06T23:00:00Z" },
      ],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/v1/browser/session") return json(session);
      if (url === `/v1/jobs/${jobID}`) return json(job);
      if (url === "/v1/browser/artifacts/result") return json({ protocol_version: 1, status: "succeeded", failure: null, result: { kind: "diff", provenance: {} } });
      throw new Error(`unexpected request ${url}`);
    }));

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "View safely" }));
    const frame = screen.getByTitle("Tracewake diff report");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame.getAttribute("src")).toBe("/v1/browser/artifacts/report?disposition=inline");
  });

  it("reconstructs a job after refresh and does not predict a cancellation winner", async () => {
    const jobID = "11111111-1111-4111-8111-111111111111";
    window.history.replaceState({}, "", `/jobs/${jobID}`);
    const running: Job = {
      job_id: jobID,
      operation: "diff",
      state: "running",
      run_ids: ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"],
      profile: "align-v1",
      current_attempt_number: 2,
      attempts: [
        { attempt_number: 1, state: "fenced", started_at: "2026-08-06T23:00:00Z", finished_at: "2026-08-06T23:01:00Z", failure: { schema_version: 1, code: "lease_lost", message: "lease expired", retryable: true } },
        { attempt_number: 2, state: "running", started_at: "2026-08-06T23:02:00Z", finished_at: null, failure: null },
      ],
      progress: { protocol_version: 1, attempt_number: 2, sequence: 4, stage: "analyzing", message: "Aligning trajectories" },
      cancel_requested_at: null,
      failure: null,
      created_at: "2026-08-06T23:00:00Z",
      updated_at: "2026-08-06T23:02:00Z",
      terminal_at: null,
      artifacts: [],
    };
    const succeeded: Job = {
      ...running,
      state: "succeeded",
      cancel_requested_at: "2026-08-06T23:03:00Z",
      terminal_at: "2026-08-06T23:03:00Z",
      updated_at: "2026-08-06T23:03:00Z",
      attempts: [running.attempts[0], { ...running.attempts[1], state: "succeeded", finished_at: "2026-08-06T23:03:00Z" }],
    };
    let cancelled = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/v1/browser/session") return json(session);
      if (url === "/v1/runs") return json({ runs: [] });
      if (url.startsWith("/v1/audit")) return json({ records: [] });
      if (url === `/v1/jobs/${jobID}/cancel` && init?.method === "POST") {
        cancelled = true;
        expect(new Headers(init.headers).get("X-Tracewake-CSRF")).toBe(session.csrf_token);
        return json(succeeded);
      }
      if (url === `/v1/jobs/${jobID}`) return json(cancelled ? succeeded : running);
      throw new Error(`unexpected request ${url}`);
    }));

    render(<App />);
    await screen.findByText("Aligning trajectories");
    expect(screen.getByText("fenced")).not.toBeNull();
    expect(screen.getByText("Attempt 2")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Request cancellation" }));
    await screen.findByText(/Cancellation requested Aug 6, 2026/);
    expect(screen.getAllByText("succeeded").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Request cancellation" })).toBeNull();
  });

  it("renders server text as text rather than active markup", async () => {
    const attack = `<img src=x onerror="window.__xss=true">`;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/v1/browser/session") return json(session);
      if (url === "/v1/runs") return json({ runs: [] });
      if (url.startsWith("/v1/audit")) return json({ records: [{ id: 1, aggregate_type: "job", aggregate_id: "11111111-1111-4111-8111-111111111111", event_type: attack, actor_type: "worker", created_at: "2026-08-06T23:00:00Z" }] });
      throw new Error(`unexpected request ${url}`);
    }));

    const { container } = render(<App />);
    await screen.findByText((content) => content.includes("<img src=x onerror="));
    expect(container.querySelector("img")).toBeNull();
    await waitFor(() => expect((window as typeof window & { __xss?: boolean }).__xss).not.toBe(true));
  });
});
