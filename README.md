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

## Local control plane

The hosted lifecycle can be exercised locally without AWS. With Go 1.24,
PostgreSQL 17, and `uv` installed, one command starts PostgreSQL, the Go
control plane, and the Python worker:

```sh
scripts/local-control-plane
```

The command prints the private credentials-file location and stops all three
processes together on Ctrl-C. Docker is an alternative:

```sh
docker compose up --build
docker compose exec controlplane cat /run/locus/credentials.json
```

The first command starts PostgreSQL, one Go control-plane process, and the
Python worker. The credentials file is created once in a private Docker volume;
copy its `token` value into `LOCUS_TOKEN`, then upload a deterministic bundle:

```sh
export LOCUS_REMOTE_URL=http://127.0.0.1:8080
export LOCUS_TOKEN=<token-from-credentials.json>
locus remote upload run.bundle.tar
locus remote runs
```

Build a bundle from an exported cassette with
`locus.bundle.build_bundle(cassette, destination)`. Remote commands are
additive: recording, replay, verification, import, export, and comparison keep
using the local SQLite store and do not require the service. Remove the local
stack and all its volumes with `docker compose down --volumes` when its retained
test data is no longer needed.

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
uv build
```

`locus diff` / `view` need `uv sync --extra embeddings` unless you pass
`--lexical`. Corpus tests skip in a fresh clone because the runs are not
committed. `tests/test_offline.py` is the load-bearing gate: a real socket is
recorded through the CLI, then replayed with connection count at zero.
