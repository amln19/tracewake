# Hosted threat model v1

The hosted system accepts sensitive recorded artifacts and analyzes them. It
does not execute uploaded agents or arbitrary user code. Replay interception
and network blocking are local correctness controls, not a hostile-code
sandbox.

## Assets and trust boundaries

Protected assets are tenant tokens, worker credentials, attempt tokens,
bundles, prompts and model output inside events, source and tool blobs, result
artifacts, presigned URLs, object identities, audit records, and authoritative
job state.

Trust boundaries are the tenant-to-public API, browser-to-public API,
public-control-plane-to-private-worker API, control-plane-to-PostgreSQL,
service-to-object-store, and outbox-to-notification transport. PostgreSQL is
trusted for lifecycle authority. Object storage is trusted for immutable bytes
only after identity verification. The queue, logs, traces, metrics, browser,
workers, and audit ledger are not state authorities.

## Tenant threats

Workspace identity comes only from authentication. Every run, normalized input,
job, artifact, audit query, and idempotency key is scoped to it. Unknown and
cross-workspace identifiers have indistinguishable responses. Composite
database keys prevent a normalized input from joining runs across workspaces.

Tokens have 256 random bits, one-time display, a non-secret prefix, explicit
scopes, optional expiry, revocation, and versioned pepper verification. Full
tokens, verifiers, and peppers are absent from logs and audit. Rate and size
limits bound guessing, enumeration, and resource consumption.

## Bundle threats

The parser rejects compression, oversized archives, excess entries or events,
traversal, absolute and ambiguous names, duplicates, links, devices, extension
headers, metadata drift, noncanonical JSON, unsupported versions, digest and
size mismatch, missing references, and extra blobs. Validation occurs before a
run is `ready`. Failed ingestion exposes no usable run and returns only a
bounded versioned code.

Bundle bytes and logical content have separate digests. Storage checksum or
object version does not replace either. Reuse of identical bytes is allowed
only inside one workspace after compatibility checks.

## Worker threats

Workers use private-network HTTPS and separate rotatable service credentials.
Claims choose work from authoritative notifications; a worker cannot choose a
workspace or arbitrary object key. Attempt tokens constrain mutation to the
current claim. PostgreSQL conditional updates fence a stolen, duplicated,
expired, or late attempt.

Workers receive short-lived access only to normalized inputs and
attempt-scoped outputs. They have no PostgreSQL credentials and cannot write an
authoritative mutable key. A compromised worker can disclose the sensitive
inputs assigned during credential validity, so concurrency, token scope,
network egress, access duration, and secret rotation remain containment
controls rather than proof of isolation.

## Browser threats

A browser never stores a permanent workspace token. A later browser session
exchange must produce a short-lived Secure, HttpOnly, SameSite cookie and use
CSRF protection on mutations. Refresh reconstructs server state through the
API. SSE contains no authority or sensitive content.

HTML results are sensitive and potentially active. They are downloaded or
served from an isolated origin with a restrictive CSP and sandbox; they are not
inserted into the application DOM as trusted markup. Artifact URLs expire
within 15 minutes and are not retained in history, logs, or local storage.

## Data minimization and retention

Logs, traces, metrics, queues, errors, progress, and audit payloads exclude
prompts, model responses, source, tool output, blob bytes, credentials,
presigned URLs, full tokens, private paths, and arbitrary stack traces.

Authoritative bundles and results expire after 90 days. Failed uploads and
orphan attempt objects expire after 24 hours. Audit records expire after 365
days. A deletion request removes API access transactionally and physical purge
finishes within 24 hours. Retention jobs verify object identity before deletion
and never delete an artifact referenced by an authoritative success row.

## Residual risks

Redaction targets known secrets and home paths but cannot prove arbitrary
source, binary, or unknown-secret safety. Authorized workers necessarily see
assigned content. Traffic analysis, cloud control-plane compromise, malicious
semantic payloads that exploit a parser, and operator access remain risks
requiring deployment hardening, patching, least privilege, monitoring, and
incident procedures. This model makes no syscall-isolation or complete
interception claim.
