# locus

Record a coding-agent run once, replay it offline, and align a good run against
a bad one to find the step where their trajectories stopped agreeing.

The installed package can record and replay a program without an API key:

```sh
work="$(mktemp -d)"
locus record --store "$work/store" --name smoke -- python -c \
  'import locus; s = locus.current(); print(s.clock.time()); s.outcome(status="ok")'
locus replay smoke --store "$work/store"
```

Both commands print the same recorded clock value. Replay-only sessions install
the network blocker even if process-wide or per-session configuration tries to
disable it.

## Results

On 41 blinded hand-labeled pairs (single annotator, synthetic injected bugs),
the aligner lands within two steps of the label on 33/41, against 12/41 for
first target-width difference, 6/41 for last common prefix, and 12/41 for a local
7B judge on the same packets. On the 29 pairs where first-difference was already
outside ±2 — the only pairs where an aligner can show a distinct win — it hit
21/29 and first-difference hit 0/29. No named baseline beat it on a single pair.

Always guessing "step 6" scores 32/41 — one below the aligner on the full set —
but on the contestable subset a constant still does better (best constant 23/29
vs aligner 21/29). Labels cluster because most failing runs here ran out of
budget while still exploring; median failing trajectory is 6 steps. The honest
claim: the aligner beats every named baseline; against a constant it wins the
full set by one pair and loses where first-difference already fails.

Ablations on the same 41: target-width argument similarity alone matches the
full distance (33/41); bag-of-words reasoning matches the pinned embedder
(33/41); linear gaps lose three pairs; dropping reasoning gains one. On this
corpus the load-bearing piece is target-width args, not affine gaps or
embeddings.

Transfer: `SWE-smith-trajectories` still yields no same-instruction pairs.
`SWE-Gym/OpenHands-Sampled-Trajectories` does — 129 same-model (gpt-4o)
OpenHands pairs under the corpus length gate after stripping `finish`. On an
80-pair blinded hand-labeled subset (single annotator) the aligner hits 36/80
within ±2 against 29/80 and 26/80 for the positional baselines, but the best
constant scores 34/80. The whole-set number is not a win.

Splitting at the median failing length separates two regimes. Under 18 steps
the aligner hits 31/40 against 23 and 21, clearing the best constant (26/40)
and beating last-common-prefix at p=0.031. Over 18 steps everything collapses:
aligner 5/40, baselines 6 and 5, and a constant beats all three at 15/40.

That collapse is systematic, not noise. On long runs the aligner answers late —
25 of 40 predictions sit past the label, median predicted step 19.5 against a
median label of 11. These are runs that go wrong early and then thrash,
re-applying the same failed edit to the same file for twenty steps. Tool and
target keep matching the successful run, so the traces only stop corresponding
near the end, while the label marks where the run stopped being recoverable.
On a thrashing run those are different places. Closing that gap is the open
problem here, and it is a question about what divergence means rather than a
parameter to tune. Scout notes and packets:
`corpus/alignment/external_scout.json`, `corpus/labels/external/`.

## Library use

```python
import locus

with locus.record("fix-off-by-one") as rec:
    model = rec.model(
        provider="acme", model_id="acme-1",
        create_fn=client.create, stream_fn=client.stream,
    )
    agent.run(task, model, rec.tools(client.dispatch), rec.clock, rec.fs)
    rec.outcome(status="ok")
    run_id = rec.run_id

with locus.replay(run_id) as rep:
    model = rep.model(provider="acme", model_id="acme-1")
    agent.run(task, model, rep.tools(), rep.clock, rep.fs)
```

Same agent code both times. Replay answers model chunks, tool results, files,
and clock values from the log with the network off. Or wrap a program you do
not want to edit:

```
locus record -- python my_agent.py
locus replay <run-id>
```

For CI, the pytest fixture `locus_cassette` defaults to replay-only (`none`).
Replay needs `PYTHONHASHSEED=0` (the CLI sets it; elsewhere locus tells you).

A runnable version of this, wired through the tool-calling shape most
OpenAI-compatible clients speak rather than this repo's own agent, is in
`examples/openai_agent.py`. `python examples/demo.py` records two variant
runs and diffs them, no API key or network needed:

```
divergence at BAD eac669ff step 2
alignment score 0.274  length ratio 1.50
embeddings lexical@unpinned

      GOOD 98ff2afd                             BAD eac669ff
------------------------------------------------------------
   =  1. get_weather → Lisbon                   1. get_weather → Lisbon
      —                                         >>> 2. get_weather → Lisbon, Portugal
   |  2. write_note → trip-notes.txt            3. write_note → error-log.txt
```

## How it works

- **Records** nondeterministic inputs consumed through Locus's supported
  adapters: model calls (including stream chunk boundaries), tool calls,
  `Session.fs` operations, and supported clock, random, UUID, and environment
  reads. This is not complete syscall, native-code, subprocess, or universal
  filesystem interception. Parallel tool batches keep an intra-batch index — a
  batch is a partial order, not a total one.
- **Matches** replay requests with `model` + `messages_hash` by default.
  `ordinal` is opt-in and never a silent fallback.
- **Modes:**

  | Mode | Behavior |
  |---|---|
  | `once` | Replay if the cassette exists, record if not |
  | `none` | Replay only; error on any new request, no network |
  | `new_episodes` | Replay what matches, record what does not |
  | `all` | Always record |

- **Redaction** is default-on and targets configured secret values, known secret
  headers and environment names, and home paths. It cannot prove arbitrary
  source, binary content, private repository data, or unknown secrets safe to
  distribute. `locus record --no-redact` turns it off; the cassette records that
  fact.
- **Cassettes** export to JSONL + blobs (`locus export` / `import`). The header
  carries model id and date so a stale cassette can warn rather than pass quietly.
  Its digest identifies canonical logical event content. Deterministic bundle
  v1 packages validated cassette content as uncompressed USTAR; its separate
  digest identifies every transport byte. Bundle production and pure validation
  are available through `locus.bundle`.

## Commands

```
locus diff <good> <bad>              # align and print the divergence step
locus view <good> <bad> -o out.html  # self-contained HTML, side-by-side + provenance
locus verify <cassette-directory>    # validate without changing the local store
locus export <run> -o cassette       # export JSONL and content-addressed blobs
locus import cassette                # validate completely, then import atomically
locus pprof <run> --view tokens      # token spend as standard pprof (needs provenance tags)
locus intervene <run> --drop-tag file_read --from-step 4 -- <agent>
locus otel <run> -o trace.json       # OTLP/JSON GenAI spans
```

`diff` / `view` use a pinned local embedder (`uv sync --extra embeddings`); pass
`--lexical` elsewhere. Divergence needs runs that end differently — a fixed
terminal action on every run can mask it.

`intervene` replays the model and re-executes the world, so the free prefix ends
at the first tool output that is not byte-identical to the recording. The bench
agent strips pytest wall-clock from suite output so a re-run does not break that
prefix on timing alone. Provenance tags on `Message`s power both the HTML
context grouping and the pprof leaves; untagged blocks collapse to one bucket.
Exported profiles carry no time, file name, or platform in their gzip header, so
the same run always exports the same bytes.

## Local control plane

The hosted lifecycle can be exercised locally without AWS. With Go 1.24,
PostgreSQL 17, Node 26, npm, and `uv` installed, one command starts PostgreSQL,
the Go control plane, and the Python worker:

```sh
scripts/local-control-plane
```

The command also builds and serves the TypeScript dashboard at
`http://127.0.0.1:8080`. Paste the private workspace token from the printed
credentials file into its one-time exchange form. The browser receives only a
15-minute HttpOnly session; the durable token is not stored by the dashboard.
The command stops all three processes together on Ctrl-C. Docker is an
alternative:

```sh
docker compose up --build
docker compose exec controlplane cat /run/locus/credentials.json
```

The first command starts PostgreSQL, one Go control-plane process, and the
Python worker, with the dashboard served by the control plane. The credentials
file is created once in a private Docker volume; use its `token` in the
dashboard or copy it into `LOCUS_TOKEN`, then upload a deterministic bundle:

```sh
export LOCUS_REMOTE_URL=http://127.0.0.1:8080
export LOCUS_TOKEN=<token-from-credentials.json>
locus remote upload run.bundle.tar
locus remote runs
```

A `ready` run can then be analyzed. `diff` compares two runs with `lexical-v1`
and produces structured JSON plus a self-contained HTML companion; `otlp` and
`pprof` each turn one run into the same OTLP/JSON spans and gzipped token
profile the local commands export:

```sh
locus remote analyze pprof <run-id> --idempotency-key spend-1
locus remote job <job-id>
locus remote artifacts <job-id>
locus remote download <artifact-id> -o tokens.pb.gz
locus remote delete <run-id>
```

Repeating a request with the same idempotency key returns the original job
rather than analyzing again. Every result artifact is immutable, scoped to the
attempt that produced it, and downloaded through a short-lived URL; `download`
refuses bytes that disagree with the digest and size the control plane
recorded. The authoritative result of each job is a canonical result envelope
naming its inputs' logical and bundle digests, object versions, schema
versions, analysis profile, Locus version, worker build, and the exact
companion artifact it produced.

Hosted analyses read the bundle, not a live session, so a run's name, task, and
session start are not part of them. Only `lexical-v1` is available; a request
naming any other profile is rejected rather than silently substituted.

Build a bundle from an exported cassette with
`locus.bundle.build_bundle(cassette, destination)`. Remote commands are
additive: recording, replay, verification, import, export, and comparison keep
using the local SQLite store and do not require the service. Remove the local
stack and all its volumes with `docker compose down --volumes` when its retained
test data is no longer needed.

Bundles and results never travel through the API itself. A client declares an
exact digest and size, receives a short-lived URL for one server-generated
object key, transfers the bytes directly, and reports the immutable object
version the store assigned. The control plane commits only that exact version
after verifying digest and size, and a run becomes usable only after mandatory
validation confirms its stored bytes match the declaration.

## Hosted deployment

`deploy/aws/` is one Terraform environment: a VPC with private subnets, a
public load balancer for tenants, a separate internal load balancer for the
worker API, ECS/Fargate services for the control plane and the Python worker,
RDS PostgreSQL, a private versioned S3 bucket, an SQS job queue with a
dead-letter queue, ECR repositories, Secrets Manager secrets, least-privilege
roles, and CloudWatch log groups. Deployment, migration, rollback, and teardown
are documented in `deploy/aws/README.md`.

The deployed system runs the same code as the local stack. Object storage
replaces the local filesystem store and the queue replaces direct outbox
polling; the lifecycle, semantics, and result schemas are unchanged. Locus
itself remains local-first: none of this is required to record, replay,
verify, import, export, or compare runs.

## Operations and failure semantics

Both services emit operational telemetry as JSON lines on standard output:
OpenTelemetry spans, and metrics in CloudWatch embedded metric format so a
deployment gets alarmable metrics from container output alone. A job
notification carries its W3C trace context, so one trace covers the request
that created the job, the outbox publication, the claim, the worker's download,
analysis and upload, and the artifact commit — across Go and Python. This
telemetry describes the services and is unrelated to the OTLP artifacts Locus
produces for a run.

Metric dimensions come from fixed sets and an unrecognised value collapses to
`other`, so the number of time series is bounded. Requests are recorded by the
route template they matched, never by the path they used. `deploy/aws/`
provisions every alarm from `deploy/aws/alarms.json`; the control-plane tests
check each custom-namespace alarm against the metrics the service actually
emits, because an alarm on a metric nothing produces stays silent and reads as
health.

Failure semantics, restated as what the system does:

* A worker that dies loses its lease. The reconciler fences the attempt, which
  can no longer report progress or commit, schedules a retry, and a replacement
  attempt produces the one authoritative result.
* Retryable failures get three attempts. After the third the job is terminally
  `failed` with `retry_exhausted` and registers no artifact.
* An artifact is refused at both boundaries that check it: the store rejects
  bytes contradicting the declaration it signed, and the commit rejects an
  identity that disagrees with what the store holds. Neither leaves a
  successful job pointing at the object.
* Terminal state is immutable, and a repeated request with its original
  idempotency key returns the original job rather than analysing again.
* Losing the database stops repair rather than guessing: the reconciler reports
  the failure and resumes when the database returns.
* Retention deadlines are enforced by the control plane, and
  `locus remote delete <run-id>` expires a run and everything derived from it
  immediately. `deploy/aws/README.md` documents retention, deletion, backup,
  and recovery.

### Measured behaviour

Every number below comes from one retained run, reproducible with
`uv run python -m evidence`; its measurements and the raw telemetry they were
computed from are in `evidence/results/`. It ran on one macOS arm64 machine
with Go 1.24.13, Python 3.13.14, PostgreSQL 17.10, one control-plane process,
and one worker. These are not scale numbers, and no AWS deployment number —
object-store latency, autoscaling, or cost — is published at all.

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

The two latency figures are dominated by the local stack's one-second outbox
poll; a deployment long-polls SQS instead. The soak's mean rose from 634 ms in
its first half to 396 ms in its second, on a single worker with a growing
database.

The same run drove five failure conditions the deployment alarms on — worker
death, retry exhaustion, an artifact contradiction, a stalled outbox, and a
database outage — and every one moved the metric its alarm watches.
CloudWatch's own evaluation engine is not exercised without a deployment, and
alarms on platform metrics such as queue depth and task counts are reported as
not observable locally.

### The demonstration

One run of the harness performs the whole correctness story in order, and
`evidence/results/measurements.json` records what each step observed:

| Step | Where it is recorded |
| --- | --- |
| Upload a deterministic bundle | `ingestion` |
| Observe mandatory validation before the run is usable | `ingestion` |
| Submit a `lexical-v1` diff with an idempotency key | `analysis_load` |
| Observe the worker claim an attempt and report progress | `worker_recovery.progress_while_running` |
| Kill the active worker | `worker_recovery.kill_to_fence_seconds` |
| Observe lease expiry, fencing, and a scheduled retry | `worker_recovery.attempts` |
| Submit a late completion from the dead attempt | `late_completion.stale_attempt_requests` |
| Observe the replacement attempt succeed | `worker_recovery.succeeded_attempts` |
| Repeat the idempotent request | `idempotent_replay` |
| Inspect result, HTML companion, artifact identity, provenance, and audit | `result_provenance` |
| Show a second workspace cannot read anything | `tenant_isolation` |
| Run local Locus with no hosted service | `local_independence` |

The same story is reachable by hand through the local control plane above:
`locus remote upload`, `runs`, `analyze`, `job`, `artifacts`, `download`, and
`delete`, with the worker stopped and started to interrupt an attempt.

## Persistent formats

Event schema 3, SQLite store schema 3, cassette directory format 1, and bundle
format 1 are independent contracts even when values coincide. Unsupported
versions are rejected with instructions to use a matching Locus version; no old
version is silently reinterpreted. `SCHEMA_VERSION` remains a compatibility
alias for `EVENT_SCHEMA_VERSION`, and `CASSETTE_FORMAT` remains an alias for
`CASSETTE_FORMAT_VERSION`.

`locus verify` checks header shape and versions, event count and dense sequence,
event schemas, the logical digest, canonical paths, and every referenced blob's
presence, digest, and size. It rejects symlinks, duplicate or unexpected blob
paths, and corruption without writing to a store. Export performs the same blob
integrity checks against local storage before publishing a destination.

## Evaluation

`bench/` clones sixteen pinned Python libraries, injects a mechanical bug, and
runs a minimal ReAct agent against a local model. Labels, scoring sheets, the
task manifest, and the per-run ledger are committed; the recorded runs are not,
so example run ids will not resolve in a fresh clone.

```
uv sync --group corpus
python -m bench setup
python -m bench build-tasks
python -m bench run
python -m bench status          # works from the committed ledger alone
python -m bench external scout  # published-trajectory inventory
python -m bench external openhands   # needs: pip install datasets
python -m bench external export      # blinded transfer packets (--extend to grow)
python -m bench external score       # after filling corpus/labels/external/labels.jsonl
```

## Development

```
uv sync
PYTHONHASHSEED=0 uv run --python 3.13 pytest
python -m locus.contracts --output contracts/schemas/v1 --check
python -m contracttest.generate_fixtures --output contracttest/fixtures/v1 --check
(cd contracttest/go && go test ./...)
(cd controlplane && go test ./...)
uv run python -m evidence --output evidence/results
uv build
```

`locus diff` / `view` need `uv sync --extra embeddings` unless you pass
`--lexical`. Corpus tests skip in a fresh clone because the runs are not
committed. `tests/test_offline.py` is the load-bearing gate: a real socket is
recorded through the CLI, then replayed with connection count at zero.
