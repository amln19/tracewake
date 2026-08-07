import type { AuditRecord, Job, Run, Session } from "./types";

const MAX_BUNDLE_SIZE = 256 * 1024 * 1024;
let csrfToken = "";

type ErrorEnvelope = { error?: { code?: string } };

export class APIError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code === "unauthenticated" ? "Your session has expired." : "The request could not be completed.");
  }
}

async function decode<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => ({}))) as T & ErrorEnvelope;
  if (!response.ok) {
    throw new APIError(response.status, body.error?.code ?? "internal");
  }
  return body;
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-Locus-CSRF", csrfToken);
  const response = await fetch(url, { ...init, headers, credentials: "same-origin" });
  return decode<T>(response);
}

export async function restoreSession(): Promise<Session> {
  const session = await request<Session>("/v1/browser/session");
  csrfToken = session.csrf_token;
  return session;
}

export async function exchangeToken(token: string): Promise<Session> {
  const session = await request<Session>("/v1/browser/sessions", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
  csrfToken = session.csrf_token;
  return session;
}

export async function endSession(): Promise<void> {
  await request<void>("/v1/browser/session", { method: "DELETE" });
  csrfToken = "";
}

export async function listRuns(): Promise<Run[]> {
  return (await request<{ runs: Run[] }>("/v1/runs")).runs;
}

export async function getRun(id: string): Promise<Run> {
  return request<Run>(`/v1/runs/${encodeURIComponent(id)}`);
}

export async function listAudit(): Promise<AuditRecord[]> {
  return (await request<{ records: AuditRecord[] }>("/v1/audit?limit=100")).records;
}

export async function uploadBundle(file: File, onState: (state: string) => void): Promise<Run> {
  if (file.size > MAX_BUNDLE_SIZE) throw new Error("Bundles are limited to 256 MiB.");
  onState("Hashing exact bundle bytes");
  const bytes = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  const digest = [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, "0")).join("");
  const pending = await request<{ run_id: string }>("/v1/browser/runs/uploads", {
    method: "POST",
    body: JSON.stringify({ bundle_format_version: 1, bundle_digest: digest, bundle_size: file.size }),
  });
  onState("Uploading through the control plane");
  await request(`/v1/browser/runs/uploads/${encodeURIComponent(pending.run_id)}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/x-tar",
      "X-Locus-Bundle-Digest": digest,
      "X-Locus-Bundle-Format": "1",
    },
    body: bytes,
  });
  onState("Queuing mandatory validation");
  return getRun(pending.run_id);
}

export async function createDiff(runIDs: [string, string]): Promise<string> {
  const result = await request<{ job_id: string }>("/v1/jobs", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ operation: "diff", run_ids: runIDs, profile: "lexical-v1" }),
  });
  return result.job_id;
}

export async function getJob(id: string): Promise<Job> {
  return request<Job>(`/v1/jobs/${encodeURIComponent(id)}`);
}

export async function cancelJob(id: string): Promise<Job> {
  return request<Job>(`/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export function subscribeToJob(id: string, onProgress: () => void): () => void {
  const events = new EventSource(`/v1/jobs/${encodeURIComponent(id)}/events`);
  events.addEventListener("progress", onProgress);
  return () => events.close();
}

export function artifactURL(id: string, inline = false): string {
  const query = inline ? "?disposition=inline" : "";
  return `/v1/browser/artifacts/${encodeURIComponent(id)}${query}`;
}

export async function readResult(id: string): Promise<unknown> {
  const response = await fetch(artifactURL(id), { credentials: "same-origin" });
  if (!response.ok) throw new APIError(response.status, "artifact_unavailable");
  return response.json();
}
