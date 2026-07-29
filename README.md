# locus

Find where two coding agent runs diverged. Record an agent run once, replay it
offline for free, then diff a good run against a bad one to get the exact step
where they stopped agreeing.

Right now locus does the first two. Record and replay work, and there is a task
suite that generates runs to work on; trajectory alignment — the part that answers
"where did these two runs stop agreeing" — is next.

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

The agent code is the same in both blocks. On replay it observes the same model
chunks, the same tool results, the same files, and the same clock values, byte
for byte, and the network is switched off — an attempted connection raises rather
than succeeding quietly.

Or wrap a program you don't want to edit:

```
locus record -- python my_agent.py
locus replay <run-id>
```

The wrapper injects itself before your program imports anything and stores the
command it ran, so replaying needs nothing but the run id.

## What gets recorded

Model calls, tool calls, filesystem reads and writes, and the clock, random,
uuid and environment values the agent consumed. The last group is intercepted by
monkeypatching `time`, `random`, `uuid` and `os.environ`; calls originating
inside the standard library are skipped, because the interpreter reads the clock
constantly on its own and how often varies between two runs of the same program.

Runs are stored as an append-only event log in SQLite, with file contents and
large tool results kept in a content-addressed blob store so a hundred runs over
the same repo store those files once. Every event names the model call that
caused it, so a run reconstructs as a tree rather than a flat list.

Streaming is recorded as the assembled response plus its chunk boundaries, and
re-emitted through the same iterator interface, so agent code that consumes a
stream still works on replay. Inter-chunk timing is recorded but deliberately
not reproduced — it isn't semantically meaningful and it would make replay slow.

Parallel tool batches are recorded under their parent model call with an
intra-batch index. A batch is a partial order, not a total one, so it replays
correctly no matter what order its calls happen to complete in.

## Matching and record modes

Replay matches a request against the log using a configurable list of matchers,
defaulting to model and message hash:

```python
locus.configure(match_on=["model", "messages_hash"])   # default
locus.configure(match_on=["model", "tool_names", "ordinal"])  # looser
```

`ordinal` matches by position in the recorded sequence. It is opt-in and never a
silent fallback — a fallback would hide exactly the divergence this tool exists
to surface. Every replay reports how many calls matched, how many matched without
proving request identity, and how many missed.

Record mode governs what gets written, independently of what gets read:

| Mode | Behavior |
|---|---|
| `once` | Replay if the cassette exists, record if not, error on anything unmatched |
| `none` | Replay only; error on any new request, guaranteed no network |
| `new_episodes` | Replay what matches, record what doesn't |
| `all` | Never replay, always record |

`new_episodes` is what lets a replayed agent that walks off the recorded path
keep going and capture the new branch instead of hard-failing.

## Redaction

On by default. Agent traces contain API keys, absolute paths with your username,
and file contents from private repos, and cassettes are meant to go in git.
Secrets are scrubbed before anything reaches disk, not on the way out of it.

```python
locus.configure(
    filter_env=["*_API_KEY", "*_TOKEN"],
    filter_headers=["authorization", "x-api-key"],
    before_record=lambda event: scrub(event),
)
```

Values are taken from the environment rather than guessed from shape, so two
machines holding different credentials in the same variable both scrub to the
same placeholder and the cassette still replays on either one. Scrubbing runs on
the matching path too, since it rewrites the content that gets hashed. `locus
record --no-redact` turns it off, and the cassette records that it was written
that way.

Redaction does mean the log holds the scrubbed value rather than the real one, so
a replayed run sees `[REDACTED]` where the recording saw a key. Recording itself
still hands the agent the real value; only the log is scrubbed.

## Cassettes

`locus export <run> -o dir/` writes JSONL plus a blob directory — reviewable in a
pull request, unlike a binary SQLite file. `locus import dir/` reads it back, and
refuses a file whose contents no longer hash to what its header claims.

The header records the model id, provider and recording date. Model weights
change under a stable model id, so replaying a months-old cassette may be
replaying a model that no longer behaves the way it did; replay says so rather
than passing silently.

## In CI

A recorded run becomes a regression test that costs nothing to re-run:

```python
def test_agent_still_fixes_the_bug(locus_cassette):
    with locus_cassette("fix-off-by-one") as session:
        agent.run(task, session.model(provider="acme", model_id="acme-1"), ...)
```

The pytest plugin defaults to record mode `none`, so the test replays and cannot
reach the network. Pass `--locus-record=all` to re-record.

Replay requires `PYTHONHASHSEED=0`: set iteration order otherwise varies between
runs and breaks determinism in agent code you don't control. The CLI sets it for
you. Everywhere else locus raises and tells you the command to run, since the
interpreter fixes the flag at startup and nothing can change it afterward.

## The task suite

`bench/` generates the runs the analysis is built on. It clones sixteen small,
pinned, permissively licensed Python libraries, injects a single mechanical bug
into one of them — a swapped comparison, a shifted bound, two arguments
exchanged, a deleted guard clause — and asks a minimal ReAct agent to fix it
from a generated bug report.

```
python -m bench setup          # clone the pinned repos, build their test env
python -m bench build-tasks    # inject bugs, validate them, write the manifest
python -m bench run            # record the agent against every task
python -m bench status         # outcome rates and how many tasks came out mixed
```

The agent runs against a local model in process, so a full corpus costs nothing
and touches no network. It reaches the model, its tools, the filesystem and the
clock only through a recording session, which is what makes every run replayable
afterwards.

Two things make the suite worth more than a pile of transcripts.

**Injected bugs come with ground truth.** A mutation only becomes a task if the
library's test suite is green before it and red after it, and if it breaks a
bounded number of tests — a mutation nothing notices is not a bug, and one that
breaks everything is a broken checkout. Because the bug was injected, the file
and line the fix belongs in are known facts rather than guesses, which is the
partial ground truth divergence localization needs.

**Bug reports never name the location.** Issue text is generated from the
observed test failures and describes only the symptom. About a third of the
issues in the public SWE-bench set contain their own solution, which makes any
later claim about which context actually mattered unfalsifiable; the agent has to
find the file by looking.

Outcomes are recorded twice. *Coverage* asks whether the run left a well-formed,
applicable patch — source that still parses, differs from the broken state, and
is not a test file. *Resolve* asks whether that patch made the suite green.
Coverage is the primary label because a small model produces patches far more
often than it produces fixes, so resolve alone gives almost no positive examples
to learn from.

Every context block the agent sends is labelled with where it came from — system
prompt, tool schema, the bug report, a file it read, test output, the repository
map, feedback on its own malformed output. The labels are free to capture while
the run happens and impossible to reconstruct afterwards, once a file's contents
and a tool's output are both just text in a transcript.

## Prior art

The record/replay design — cassettes, request matchers, record modes, before-
record redaction, staleness intervals — is borrowed from VCR.py and Ruby's VCR,
which solved this for HTTP fifteen years ago. What's new here is applying it to
model calls, tool calls, the filesystem and the clock, and using it as the
substrate for trajectory alignment.

## Development

```
uv sync
PYTHONHASHSEED=0 uv run pytest
```

`tests/test_offline.py` is the load-bearing test: an agent whose model backend
genuinely opens a socket is recorded through the CLI, then replayed. The test
server counts connections, and the replay must reproduce the run byte for byte
with that count at zero.
