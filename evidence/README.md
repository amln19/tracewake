# Operational evidence

Every operational number Tracewake publishes is produced by this harness and
retained in `results/`. Reproduce it with:

```sh
uv run python -m evidence --output evidence/results
```

The harness needs `go`, `uv`, and a local PostgreSQL 17 installation
(`initdb`, `pg_ctl`, `psql`, `pg_dump`, `pg_restore`) on `PATH`. It needs no
AWS account, no network access, and no credentials. It builds the control
plane, initialises a throwaway database under `.tracewake/evidence`, starts the
control plane and a Python worker, and drives them through every scenario
below before tearing the whole thing down.

Runs take roughly ten minutes. Several scenarios wait out real timers — a
60-second attempt lease, 5- and 30-second retry backoffs — because shortening
them would measure a different system than the one that gets deployed.

The retained local run used one control-plane process, one worker, PostgreSQL
17, Go, and Python 3.13 on one macOS arm64 machine.

## What each scenario measures

| Scenario | What it does | What it establishes |
| --- | --- | --- |
| `ingestion` | Uploads bundles and waits for mandatory validation | A run is not analysable until Python has validated its bytes |
| `analysis_load` | Submits a batch of `align-v2` diffs over distinct run pairs | Throughput and end-to-end job latency under a burst |
| `soak` | Holds a steady submission rate | Latency does not drift between the first and second half |
| `hosted_matches_local` | Compares a hosted OTLP artifact with a local export | Hosted analysis agrees byte-for-byte with local Tracewake |
| `idempotent_replay` | Repeats one request with its original key | The same logical job comes back, not a second one |
| `worker_recovery` | SIGKILLs the process group holding an attempt | Lease expiry, fencing, retry, and one authoritative result |
| `late_completion` | Heartbeats stop; the fenced attempt then tries to commit | A stale attempt cannot report progress or commit |
| `retry_exhaustion` | Fails every allowed attempt retryably | Terminal `retry_exhausted` with no artifacts registered |
| `artifact_mismatch` | Contradicts an artifact declaration at upload and at commit | Neither boundary lets a wrong object become authoritative |
| `outbox_backlog` | Leaves a notification unconsumed | The backlog age an operator alarms on becomes visible |
| `service_resumes` | Submits normal work after the faults | Injected faults leave no lasting damage |
| `reconciler_failure` | Stops PostgreSQL under a running control plane | The reconciler reports failure instead of guessing |
| `tenant_isolation` | Reads the first workspace's records as a second workspace | No run, job, or audit record crosses the boundary |
| `backup_and_restore` | Dumps, drops, and reloads the database | Authoritative state survives a restore |
| `migration` | Migrates an empty database twice | Migrations apply in order and the second pass is a no-op |
| `local_independence` | Records and replays with no service running | Local Tracewake needs none of this |

## Reading `results/measurements.json`

* `scenarios` — the raw observations, one key per scenario above.
* `latency` — percentiles over server-side durations. Durations come from
  database timestamps rather than from when the harness polled.
* `telemetry` — span and metric counts, including how many traces span both
  the Go control plane and the Python worker.
* `alarms` — every alarm in `deploy/aws/alarms.json` evaluated against the
  metric stream this run produced.

`control-plane.jsonl` and `worker.jsonl` are the complete telemetry streams the
two services emitted, retained so the summary can be recomputed.

### Measured behaviour

| Measurement | Value |
| --- | --- |
| 10 bundles uploaded and validated | p50 879 ms, p95 950 ms |
| 24 diff analyses submitted at once | drained in 1.47 s |
| Their end-to-end latency | p50 1153 ms, p95 1237 ms |
| One analysis every two seconds for a minute | 30 of 30 succeeded, p50 460 ms |
| Killed worker to fenced attempt | 60.4 s, the attempt lease |
| Killed worker to committed result | 70.3 s |
| Spans emitted | 1986 across 636 traces |
| Traces spanning both languages | 78, up to 20 spans each |
| Distinct metric series | 103 |

The local stack polls the outbox once per second, so that interval dominates
these latency figures. Under the sustained rate, the mean was 634 ms across its first half
and 396 ms across its second; the one worker kept up rather than falling behind.
Every injected failure condition moved the metric its configured deployment
alarm watches.

### On a deployed environment

One deployment of the same release and fault workflow is retained in
[`results/aws/measurements.json`](results/aws/measurements.json).

| Measurement | Value |
| --- | --- |
| Notification latency, diff (5 samples) | 38–820 ms |
| Notification latency, mandatory validation (6 samples) | 81–923 ms |
| Fastest diff, request to terminal state | 371 ms |
| Database point-in-time restore to available | 15 min 30 s |

Three real-fault alarms entered `ALARM`: an attempt-lease loss under worker
partition, a reconciler failure during a database reboot, and worker capacity
reduced to zero. Both partitioned jobs recovered on their second attempt and
committed one authoritative result. SQS long-polling gave a 38 ms best
notification latency, unlike the local polling floor. Scaling behaviour and
cost remain unmeasured.

### Lifecycle coverage

The local evidence run exercises this complete lifecycle; the named
observations are retained in `results/measurements.json`.

| Step | Recorded observation |
| --- | --- |
| Upload a deterministic bundle | `ingestion` |
| Observe mandatory validation before it is usable | `ingestion` |
| Submit an idempotent align-v2 diff | `analysis_load` |
| Observe a claimed attempt reporting progress | `worker_recovery.progress_while_running` |
| Kill the active worker and wait for fencing | `worker_recovery.kill_to_fence_seconds` |
| Observe retry and the replacement result | `worker_recovery.succeeded_attempts` |
| Send a late completion from the old worker | `late_completion.stale_attempt_requests` |
| Repeat the idempotent request | `idempotent_replay` |
| Inspect artifact identity, provenance, and audit | `result_provenance` |
| Attempt a cross-workspace read | `tenant_isolation` |
| Record and replay with no service running | `local_independence` |

## What this does not measure

Alarm evaluation here applies the deployed thresholds to the metrics the
services actually emitted. It does not exercise CloudWatch's evaluation engine,
and alarms on platform metrics — queue depth, task counts, database storage —
are reported as not observable locally, because nothing local publishes them.

Object-store latency, autoscaling behaviour, and cost need a deployed
environment and are not measured or published.

Local notification delivery polls the outbox once a second, so queue latency
measured here is dominated by that interval rather than by the work. A hosted
deployment uses SQS long-polling instead.

## A note on the profile name

The retained run in `results/` records its analysis profile as `lexical-v1`.
That profile was renamed twice afterwards: to `align-v1`, because the old name
described the similarity function rather than what the profile produces and
collided with the unrelated `--lexical` embedder flag; and then to `align-v2`,
when its `divergence` field changed to report the single-trace rule. The
measurement is not edited to match: it records what ran. Reproducing the harness
today writes `align-v2` instead, along with fresh timings.
