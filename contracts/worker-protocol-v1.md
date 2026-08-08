# Worker protocol v1

The worker protocol is JSON over private HTTPS under `/internal/v1`. It is not
reachable through the public listener. Messages reject unknown fields and use
the generated version 1 schemas. Workers call Tracewake through Python APIs and
do not parse CLI output or connect to PostgreSQL.

## Authentication

Workers use a credential distinct from tenant tokens. The bearer secret is at
least 256 random bits, rotatable with overlapping current and previous keys,
and scoped to the worker endpoints. Its verifier is stored using a versioned
HMAC-SHA-256 server pepper and compared in constant time. The credential is
delivered through the runtime secret mechanism and sent only over private
HTTPS. A worker cannot name a workspace or choose arbitrary input objects.

Claim returns an additional random attempt token. Its verifier is stored on the
attempt row. Every attempt endpoint requires both worker authentication and
`Tracewake-Attempt-Token`. Attempt tokens are never returned again, logged,
audited, placed in queue payloads, or used for another attempt.

## Notification and claim

Queue payloads use `job-notification.schema.json` and contain only protocol
version, job ID, job version, and operation.

`POST /internal/v1/claims` accepts `claim-request.schema.json`. The authenticated
worker ID must match the body. A successful conditional claim returns
`claim.schema.json`, HTTP 201, normalized immutable input artifact references,
the attempt number and token, the database lease expiry, operation, and exact
profile. It never returns a workspace selector.

HTTP 200 with current durable state means the notification is duplicate or
obsolete and may be deleted. HTTP 409 means another current attempt won and may
be deleted after the response. HTTP 422 means an incompatible notification and
must redrive. HTTP 503 represents uncertainty and must not cause deletion.

## Attempt operations

All paths include the claim's job ID and attempt number:

* `PUT /internal/v1/jobs/{job}/attempts/{attempt}/heartbeat` accepts the
  heartbeat schema and returns the new database lease expiry. The worker sends
  it every 20 seconds. HTTP 409 `lease_lost` stops work.
* `PUT /internal/v1/jobs/{job}/attempts/{attempt}/progress` accepts the progress
  schema. Sequences strictly increase. The control plane stores only the newest
  bounded snapshot. A duplicate sequence is idempotent only when bytes match;
  a changed duplicate is invalid.
* `GET /internal/v1/jobs/{job}/attempts/{attempt}/cancellation` returns
  `{"protocol_version":1,"cancel_requested":boolean}`. A worker also stops on
  lease loss without waiting for cancellation.
* `POST /internal/v1/jobs/{job}/attempts/{attempt}/artifacts` declares kind,
  media type, and expected digest and size. It returns a server-created
  attempt-scoped key, required SHA-256 checksum, short-lived upload URL and
  method, any headers the URL's signature covers, and expiry. The worker cannot
  supply or alter the key. It reports the immutable object version the store
  returns; a local deployment reports the content digest. A declaration over
  64 MiB is rejected; the operation's own limits keep outputs well below it.
* `GET /internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact}` returns the
  immutable input reference and a short-lived download URL for it. Input bytes
  never pass through the control plane.
* `GET /internal/v1/identity` returns the authenticated worker ID for claims,
  so a worker that received only a credential needs no second configured value.
* `POST /internal/v1/jobs/{job}/attempts/{attempt}/complete` accepts the
  artifact-commit schema after upload. The control plane verifies immutable
  object version, digest, size, semantic schema, and canonical result before
  the single success transition.
* `POST /internal/v1/jobs/{job}/attempts/{attempt}/fail` accepts the failure
  schema. The control plane, not the worker, applies retry policy and terminal
  state.

Progress and heartbeat return HTTP 204 on success. Completion returns HTTP 200
with `result-envelope.schema.json` for the authoritative result. Failure returns
HTTP 202 when retry was durably scheduled or HTTP 200 for terminal failure.
Stale, expired, superseded, cancelled, or terminal attempts return HTTP 409
`lease_lost`; callers stop and never infer state from the object store.

## Acknowledgement and shutdown

The worker deletes a notification only after observing the corresponding
durable fact in the acknowledgement matrix. It renews queue visibility only
while heartbeat proves the database lease current. On shutdown it stops new
claims, cancels active semantic work, reports a bounded retryable failure when
the lease remains valid, and otherwise abandons the message for redelivery.

Requests and errors exclude bundle contents, prompts, source, tool output,
blobs, private paths, credentials, attempt tokens, presigned URLs, and stack
traces. Failure messages are safe bounded summaries; detailed diagnostics stay
in sensitive attempt artifacts when explicitly supported.
