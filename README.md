# locus

Find where two coding agent runs diverged. Record a run once, replay it offline
for free, then align a good run against a bad one to get the step where they
stopped agreeing.

```
$ locus diff 16ccfac0 45af6f8f

divergence at BAD 45af6f8f step 9
alignment score -1.080  length ratio 1.07
embeddings mlx-community/bge-small-en-v1.5-bf16@0e415031434cdf5f1b89d584e11be33b82abfc8d

      GOOD 16ccfac0                             BAD 45af6f8f
------------------------------------------------------------
   =  1. run_tests                              1. run_tests
   =  2. read_file → bidict/_orderedbase.py     2. read_file → bidict/_orderedbase.py
      3. read_file → bidict/_orderedbase.py     —
   =  4. edit_file → bidict/_orderedbase.py     3. edit_file → bidict/_orderedbase.py
   |  5. run_tests                              4. read_file → tests/test_bidict.py
   |  6. search → WeakAttr                      5. search → test_orderedbidict_weakattr_
   |  7. read_file → bidict/_orderedbase.py     6. read_file → tests/test_bidict.py
   =  8. read_file → bidict/_orderedbase.py     7. read_file → bidict/_orderedbase.py
   =  9. edit_file → bidict/_orderedbase.py     8. edit_file → bidict/_orderedbase.py
      10. run_tests                             —
      11. search → WeakAttr                     —
      12. edit_file → bidict/_orderedbase.py    —
      13. run_tests                             —
      14. edit_file → bidict/_orderedbase.py    —
      15. run_tests                             —
   |  16. read_file → bidict/_orderedbase.py    >>> 9. read_file → tests/test_bidict.py
      —                                         10. read_file → tests/test_bidict.py
      —                                         11. read_file → tests/test_bidict.py
      —                                         12. read_file → tests/test_bidict.py
      —                                         13. read_file → tests/test_bidict.py
      —                                         14. read_file → tests/test_bidict.py
      —                                         15. read_file → tests/test_bidict.py
```

`=` agree, `|` differ, `—` one side skipped a step, `>>>` divergence. An extra
read on the good side shifts every later index, so comparing position by position
does not work — that is why the traces are aligned first.

## Results

On 41 blinded hand-labeled pairs (single annotator, synthetic injected bugs),
the aligner lands within two steps of the label on 32/41, against 12/41 for
first target-width difference, 6/41 for last common prefix, and 12/41 for a local
7B judge on the same packets. On the 29 pairs where first-difference was already
outside ±2 — the only pairs where an aligner can show a distinct win — it hit
20/29 and first-difference hit 0/29. No named baseline beat it on a single pair.

Always guessing "step 6" also scores 32/41, and on the contestable subset a
constant does better (best constant 23/29 vs aligner 20/29). Labels cluster
because most failing runs here ran out of budget while still exploring; median
failing trajectory is 6 steps. The honest claim: the aligner beats every named
baseline and does not beat a constant.

Ablations on the same 41: target-width argument similarity alone matches the
full distance (32/41); bag-of-words reasoning matches the pinned embedder
(32/41); linear gaps lose three pairs; dropping reasoning gains one. On this
corpus the load-bearing piece is target-width args, not affine gaps or
embeddings. Transfer to published SWE-agent rollouts is untested — that corpus
yields no same-instruction pass/fail pairs.

## Quick start

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

## How it works

- **Records** every nondeterministic input the agent consumes: model calls
  (including stream chunk boundaries), tool calls, filesystem reads/writes, and
  clock / random / uuid / env values. Parallel tool batches keep an intra-batch
  index — a batch is a partial order, not a total one.
- **Matches** replay requests with `model` + `messages_hash` by default.
  `ordinal` is opt-in and never a silent fallback.
- **Modes:**

  | Mode | Behavior |
  |---|---|
  | `once` | Replay if the cassette exists, record if not |
  | `none` | Replay only; error on any new request, no network |
  | `new_episodes` | Replay what matches, record what does not |
  | `all` | Always record |

- **Redaction** is default-on (API keys, home paths). Secrets are scrubbed before
  disk. `locus record --no-redact` turns it off; the cassette records that fact.
- **Cassettes** export to JSONL + blobs (`locus export` / `import`). The header
  carries model id and date so a stale cassette can warn rather than pass quietly.

## Commands

```
locus diff <good> <bad>              # align and print the divergence step
locus view <good> <bad> -o out.html  # self-contained HTML, side-by-side + provenance
locus pprof <run> --view tokens      # token spend as standard pprof (needs provenance tags)
locus intervene <run> --drop-tag file_read --from-step 4 -- <agent>
locus otel <run> -o trace.json       # OTLP/JSON GenAI spans
```

`diff` / `view` use a pinned local embedder (`uv sync --extra embeddings`); pass
`--lexical` elsewhere. Divergence needs runs that end differently — a fixed
terminal action on every run can mask it.

`intervene` replays the model and re-executes the world, so the free prefix ends
at the first tool output that is not byte-identical to the recording. Provenance
tags on `Message`s power both the HTML context grouping and the pprof leaves;
untagged blocks collapse to one bucket.

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
```

## Development

```
uv sync
PYTHONHASHSEED=0 uv run pytest
```

`locus diff` / `view` need `uv sync --extra embeddings` unless you pass
`--lexical`. Corpus tests skip in a fresh clone because the runs are not
committed. `tests/test_offline.py` is the load-bearing gate: a real socket is
recorded through the CLI, then replayed with connection count at zero.
