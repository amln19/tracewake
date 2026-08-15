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

## What each scenario measures

| Scenario | What it does | What it establishes |
| --- | --- | --- |
| `ingestion` | Uploads bundles and waits for mandatory validation | A run is not analysable until Python has validated its bytes |
| `analysis_load` | Submits a batch of `align-v1` diffs over distinct run pairs | Throughput and end-to-end job latency under a burst |
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
That profile was later renamed `align-v1`, because the old name described the
similarity function rather than what the profile produces, and collided with
the unrelated `--lexical` embedder flag. The measurement is not edited to match:
it records what ran. Reproducing the harness today writes `align-v1` instead,
along with fresh timings.
