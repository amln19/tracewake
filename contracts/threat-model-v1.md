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

Workspace identity comes only from authentication. Deletion is workspace-scoped
like every other operation: a request naming another workspace's run is
indistinguishable from a request naming one that does not exist, and changes
nothing. Every run, normalized input,
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

A browser never stores a permanent workspace token. Browser session exchange
produces a 15-minute opaque Secure, HttpOnly, SameSite=Strict cookie, while the
server stores only its keyed verifier. A per-session CSRF token is required on
mutations and rotated when a refreshed page reconstructs its session. SSE
contains no authority or sensitive content.

HTML results are sensitive and potentially active. The dashboard renders them
only through an authenticated control-plane response with a restrictive CSP
inside a sandboxed frame; they are never inserted into the application DOM as
trusted markup. Other dashboard artifact responses are attachments and are not
sniffed. Result object keys, versions, buckets, and signed storage URLs are
absent from the browser surface. Bundle bytes also pass through a same-origin,
CSRF-protected control-plane endpoint, so the browser never receives an object
store capability or infrastructure credential.

## Telemetry threats

Operational telemetry is an egress path and is treated as one. Spans carry
server-generated identifiers, route templates, attempt numbers, and operation
names; metric dimensions come from fixed sets, and an unrecognised value
collapses to `other` rather than opening a new series. Nothing derived from
tenant content reaches either stream: no token, attempt token, object key,
signed URL, content digest, prompt, blob, private path, or stack trace. A
request is recorded by the route it matched, never by the path it used, so an
identifier cannot arrive through a URL.

A notification carries W3C trace context so one trace spans both languages.
Trace and span identifiers are random and confer no authority; a worker
presenting one gains nothing without its attempt token.

The retained operational run is checked for exactly these leaks, so the claim
rests on the bytes two real services emitted rather than on review alone.

## Data minimization and retention

Logs, traces, metrics, queues, errors, progress, and audit payloads exclude
prompts, model responses, source, tool output, blob bytes, credentials,
presigned URLs, full tokens, private paths, and arbitrary stack traces.

Authoritative bundles and results expire after 90 days. Failed uploads and
orphan attempt objects expire after 24 hours. Idempotency records expire after
24 hours, published notifications after 7 days, and audit records after 365
days. The control plane enforces these windows itself, because it is the only
component that knows what a successful job still references.

A deletion request expires the run and everything derived from it in the
transaction that records it, so API access ends immediately; object cleanup
removes the bytes within the purge window. Retention never deletes a row an
authoritative success references: the artifact row is the record of what a job
committed and outlives the bytes it describes, which is what lets provenance
stay truthful after data is gone.

## Residual risks

Redaction targets known secrets and home paths but cannot prove arbitrary
source, binary, or unknown-secret safety. Authorized workers necessarily see
assigned content. Traffic analysis, cloud control-plane compromise, malicious
semantic payloads that exploit a parser, and operator access remain risks
requiring deployment hardening, patching, least privilege, monitoring, and
incident procedures. This model makes no syscall-isolation or complete
interception claim.
