# Public API v1

The API base is `/v1`. JSON requests and responses use UTF-8 and reject unknown
fields. Timestamps are RFC 3339 with an offset. IDs are UUIDs. Digests are
lowercase SHA-256 hexadecimal strings. A caller never supplies a workspace ID;
the server resolves it from authentication.

## Authentication

Tenant requests use `Authorization: Bearer <token>` over HTTPS. A token contains
a non-secret prefix and a 256-bit random secret. The database stores the prefix,
a key-version identifier, and `HMAC-SHA-256(pepper, token)`; comparison is
constant-time. New tokens use the current pepper. During rotation the server
accepts the current and immediately previous pepper until every retained token
is migrated or revoked. Peppers live outside PostgreSQL.

Authentication failures return the same response whether the prefix, token,
workspace, run, job, or artifact is unknown. Scopes are `runs:read`,
`runs:write`, `jobs:read`, `jobs:write`, `artifacts:read`, and `audit:read`.

## Errors

Errors have this shape:

```json
{"error":{"code":"invalid_request","message":"bounded safe text","request_id":"uuid"}}
```

Codes are `invalid_request`, `unauthenticated`, `forbidden`, `not_found`,
`conflict`, `idempotency_conflict`, `unsupported_version`, `rate_limited`, and
`internal`. Messages are at most 512 characters and exclude object keys,
private paths, URLs, tokens, prompts, source, blobs, and stack traces.

## Runs and mandatory ingestion

`POST /v1/runs/uploads` with `runs:write` creates an upload in `pending` state.
The request declares bundle format, exact byte size, and exact bundle digest.
The response supplies a server-generated upload identity, object key hidden
behind a short-lived upload URL, required checksum, any headers that URL's
signature covers, and expiry. Maximum size is the bundle v1 limit. Bytes go to
the object store, never through the API.

`POST /v1/runs/uploads/{upload_id}/complete` records the immutable object
version and queues mandatory validation transactionally. It is idempotent for
the same object identity. It rejects a changed identity. The run progresses
through `pending`, `uploaded`, `validating`, then `ready` or `invalid`. No API,
worker, administrator, or same-digest optimization can use a run before
`ready`. Same-bundle reuse is workspace-local and only reuses a previously
validated compatible immutable object.

`GET /v1/runs` returns a cursor page of workspace-owned run summaries.
`GET /v1/runs/{run_id}` returns ingestion state, declared and validated
versions, logical and bundle digests when known, event count, bounded failure,
creation time, readiness time, and retention deadline. Cross-workspace and
unknown IDs both return `not_found`.

## Jobs

`POST /v1/jobs` requires `jobs:write` and `Idempotency-Key` of 1–255 visible
ASCII characters. The body is the generated `public-job-request` schema.
`diff` requires exactly two distinct `ready` run IDs and `lexical-v1`; `otlp`
and `pprof` require exactly one `ready` run and no profile. Normalization binds
operation, ordered run identities, their immutable versions, and profile. An
identical key within 24 hours returns the original job with HTTP 200; a new key
creates the job with HTTP 201; changed reuse returns HTTP 409
`idempotency_conflict`.

`GET /v1/jobs/{job_id}` with `jobs:read` returns normalized inputs, state,
current attempt number, attempt summaries, current bounded progress, cancel
request time, terminal failure, authoritative artifact references, provenance,
and timestamps. States are `queued`, `running`, `retry_wait`, `succeeded`,
`failed`, and `cancelled`.

`POST /v1/jobs/{job_id}/cancel` with `jobs:write` records an idempotent
cancellation request. It returns the current job. A terminal job is unchanged.
Cancellation resolution follows the lifecycle conditional transition rather
than response arrival order.

`GET /v1/jobs/{job_id}/events` serves SSE with `jobs:read`. Events are bounded
progress snapshots and lifecycle hints, carry monotonic per-job sequence IDs,
and contain no source or tool content. SSE is not durable authority. After
reconnect or refresh, clients reconstruct state with `GET /v1/jobs/{job_id}`.

## Artifacts and audit

A successful job registers one authoritative result artifact holding the
canonical `result-envelope` JSON, plus the companion the analysis produced:
`diff_json` with `diff_html`, `otlp_result_json` with `otlp_json`, and
`pprof_result_json` with `pprof`. The envelope names its companion's exact
object identity, digest, and size. One attempt output is at most 64 MiB.

`GET /v1/artifacts/{artifact_id}/download` with `artifacts:read` checks current
workspace ownership and retention, then returns a download URL valid for at
most 15 minutes. The URL serves the artifact as an attachment rather than
inline. The URL is never included in logs, audit payloads, or SSE.

`GET /v1/audit?cursor=...&limit=...` with `audit:read` returns at most 100
workspace-scoped meaningful lifecycle records per page. It excludes heartbeat
and routine progress. Payloads contain bounded identifiers, versions, state,
and failure codes only.

All list cursors are opaque, stable within the requested order, and scoped to
the authenticated workspace. Requested deletion makes affected runs and
artifacts unreadable immediately; physical objects are purged within 24 hours.
