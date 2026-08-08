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

### Browser sessions

`POST /v1/browser/sessions` is the only dashboard request that accepts a
durable tenant token. It exchanges that token for a 15-minute opaque session,
sets it in a `Secure`, `HttpOnly`, `SameSite=Strict`, host-only cookie, and
returns a per-session CSRF token. The database stores only keyed verifiers for
both values. Browser code keeps the CSRF value in memory and never stores the
durable token or session token.

`GET /v1/browser/session` authenticates the cookie, rotates the CSRF token, and
returns the session expiry and scopes. A refresh therefore reconstructs browser
state without browser storage. `DELETE /v1/browser/session` revokes the session
and expires the cookie. Every other mutating request authenticated by a browser
session must send the current token in `X-Tracewake-CSRF`; bearer-authenticated
API clients are unchanged. Browser-session additions are backward-compatible API
v1 extensions. Unknown fields remain rejected.

Dashboard uploads use `POST /v1/browser/runs/uploads` for the declaration and
`PUT /v1/browser/runs/uploads/{run_id}` for the exact bundle bytes. Both
requests require the session cookie and current CSRF token. The `PUT` binds the
declared content length, digest, and bundle format, streams through the control
plane, and returns `validating`; it never returns an object key, version,
bucket, signed URL, or other storage capability. Durable bearer clients retain
the upload-grant protocol below.

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

`DELETE /v1/runs/{run_id}` with `runs:write` is a tenant deletion request. It
answers `204` once the run and every artifact derived from it have stopped
being readable, listable, analysable, and retained; object cleanup then removes
the stored bytes. Jobs and audit records survive as the record that the work
happened, with nothing left to download. A run that is already deleted or is
not owned by the workspace returns `not_found`.

`GET /v1/runs` returns a cursor page of workspace-owned run summaries.
`GET /v1/runs/{run_id}` returns ingestion state, declared and validated
versions, logical and bundle digests when known, event count, bounded failure,
creation time, readiness time, and retention deadline. Cross-workspace and
unknown IDs both return `not_found`.

## Jobs

`POST /v1/jobs` requires `jobs:write` and `Idempotency-Key` of 1–255 visible
ASCII characters. The body is the generated `public-job-request` schema.
`diff` requires exactly two distinct `ready` run IDs and `lexical-v1`; `otlp`
and `pprof` require exactly one `ready` run and no profile. An operation or
analysis profile this deployment does not implement returns HTTP 422
`unsupported_version` and is never replaced by a supported one; a malformed
request returns HTTP 400 `invalid_request`; a run that is unknown, owned by
another workspace, or not yet `ready` returns HTTP 409 `conflict`. Normalization binds
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

`GET /v1/browser/artifacts/{artifact_id}` accepts browser-session
authentication and streams one authorized artifact through the control plane,
without exposing its object key, version, bucket, or signed storage URL. The
default disposition is attachment with `nosniff`. Only a `diff_html` artifact
with media type `text/html` accepts `?disposition=inline`; that response has a
restrictive CSP and sandbox and is intended for a sandboxed dashboard frame.

`GET /v1/audit?cursor=...&limit=...` with `audit:read` returns at most 100
workspace-scoped meaningful lifecycle records per page. It excludes heartbeat
and routine progress. Payloads contain bounded identifiers, versions, state,
and failure codes only.

All list cursors are opaque, stable within the requested order, and scoped to
the authenticated workspace. Requested deletion makes affected runs and
artifacts unreadable immediately; physical objects are purged within 24 hours.

## Retention

Retention deadlines are visible on every run and artifact the API returns.
Authoritative input bundles and result artifacts are retained 90 days, failed
uploads and orphan attempt outputs 24 hours, idempotency records 24 hours, and
audit records 365 days. The control plane enforces those windows on its own
schedule.

Past its deadline an artifact stops appearing on its job and stops being
downloadable, and a run stops being readable, listable, and analysable. Rows a
successful job depends on are not removed: the artifact row remains the record
of exactly what that job committed, including its digest, size, and object
version, after the bytes are gone.
