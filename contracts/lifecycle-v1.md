# Hosted lifecycle v1

PostgreSQL job rows are authoritative. Queue messages wake workers, audit rows
describe meaningful transitions, and artifact objects hold bytes; none can
override current database state. `lifecycle-v1.json` is the complete transition
table and supplies the normative preconditions and postconditions.

## Attempts and leases

A job has at most one current attempt. Attempt numbers begin at one and only
increase. A claim conditionally moves `queued` to `running`, creates the next
attempt, installs a verifier for its random attempt token, and sets a lease to
database time plus 60 seconds. Workers heartbeat every 20 seconds. A heartbeat
extends only the matching current, unexpired attempt.

Queue visibility is extended only after a successful heartbeat and never
beyond the database lease plus transport allowance. Losing queue visibility
does not revoke a lease. Keeping queue visibility does not preserve an expired
lease.

A stale number, bad token, expired lease, superseded attempt, or terminal job
cannot update progress, register an authoritative artifact, or complete. The
attempt may continue physically, but all commits are fenced by one PostgreSQL
conditional transition.

## Retry and reconciliation

There are at most three attempts. Retryable failure or lease expiry after
attempt one schedules retry at database time plus 5 seconds; after attempt two,
plus 30 seconds. Failure after attempt three is terminal. Compatibility,
authorization, invalid-bundle, invalid-result, and semantic failures are
permanent. Dependency uncertainty and artifact upload uncertainty are
retryable only when no authoritative commit was observed.

Retry scheduling, its audit row, and its outbox row are transactional. The
reconciler uses bounded batches and row locking with skip-locked or equivalent
compare-and-swap predicates. It handles expired leases, due retries, stranded
queued jobs, unpublished or ambiguous outbox rows, duplicate terminal
notifications, missing notifications, and orphan attempt artifacts. Repeated
reconciliation produces no additional authoritative work.

## Cancellation linearization

Cancellation first records an idempotent request on a nonterminal job. The
resolution update and successful completion update both predicate on the same
current nonterminal row version. Exactly one update can win. If cancellation
wins, it fences the attempt and completion is stale. If completion wins, the
terminal success and exact artifact identity are immutable and cancellation
cannot rewrite them.

## Notification acknowledgement

| Durable observation | Message action |
| --- | --- |
| Terminal job | delete duplicate |
| Valid current attempt already exists | delete duplicate |
| Message names a superseded version | delete obsolete message |
| Claim succeeds | retain and extend while lease is current |
| Database, API, or object-store outcome is uncertain | do not delete |
| Malformed or incompatible notification | make no job transition; redrive to DLQ |
| Retry scheduling commits with outbox | delete current message |
| Terminal failure or cancellation commits | delete message |
| Artifact identity and success commit | delete message |
| DLQ receipt | inspect database; republish through outbox or fail by policy |

## Idempotency and outbox

An idempotency key is scoped to authenticated workspace and operation for 24
hours. It binds the normalized request digest and response identity. Identical
reuse returns the original logical response. Different reuse returns an
idempotency conflict and creates nothing.

Every transition requiring notification creates its outbox row in the same
transaction. Publishers claim rows concurrently, publish only identifiers and
versions, and mark publication afterward. A crash between publish and marking
may duplicate a message; it cannot duplicate authoritative work. Publication
metadata never changes job state.

## Immutable artifact commit

The control plane creates an attempt-scoped object key. The worker uploads,
then reports object key, immutable version, SHA-256 digest, size, media type,
schema, and canonical semantic result. Completion verifies those values and
conditionally writes both the authoritative artifact reference and terminal
success. There is no mutable `latest` key. A stale object remains orphaned and
is deleted after 24 hours unless an authoritative row references it.
