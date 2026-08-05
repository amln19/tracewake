# locus

Find where two coding agent runs diverged. Record an agent run once, replay it
offline for free, then diff a good run against a bad one to get the exact step
where they stopped agreeing.

Record, replay, and trajectory alignment all work. Given two runs of the same
task, `locus diff` aligns their tool traces with an affine-gap algorithm and
reports the step where they stopped agreeing. From there, `locus view` writes a
self-contained HTML comparison, `locus pprof` exports token spend as a standard
pprof profile, and `locus intervene` re-runs a recording with one class of
context removed to see what changes. No harness adapter yet — the recorder is
library-first, and wrapping a third-party harness is the next thing rather than
a done thing.

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

`=` the two runs agree, `|` they differ, `—` one of them did something the other
skipped, `>>>` the divergence. Note step 4 of the failing run lining up with step
3 of the passing one: the extra read at good-3 shifts everything after it, which
is why comparing position by position does not work. Both runs edit the same file
twice and stay together through step 8. At step 9 the failing run stops touching
the library and spends the rest of its budget re-reading the test file, while the
passing run keeps cycling edit-and-test until it lands the fix.

On 41 blinded hand-labeled pairs (single annotator, one pass, synthetic injected
bugs), the aligner lands within two steps of the label on 32/41, against 12/41
for first target-width difference (what simple session-diff tools do), 6/41 for
last common prefix, and 12/41 for a local 7B judge on the same packets. On the
29 pairs where first-difference was already outside ±2 of the label — the only
pairs where an aligner can show a distinct win — it hit 20/29 and
first-difference hit 0/29. No baseline beat it on a single pair in any subset.

**The caveat that matters more than the table.** Always guessing "step 6" also
scores 32/41. On the contestable subset a constant does *better* than the
aligner: step 6 gets 22/29 and the best constant, step 7, gets 23/29, against
the aligner's 20/29. The labels cluster because most of these failing runs did
not take a wrong turn — they simply ran out of budget while still exploring, so
the answer is usually the last step, and trajectories are short (median failing
run: 6 steps). So the honest claim is that the aligner beats
every *named* baseline decisively and does not beat a constant. Anyone who cites
the 32/41 without that sentence is overselling it.

Annotator self-agreement was not measured, so there is no test-retest ceiling on
these numbers.

**Transfer to real-world trajectories is untested, and an attempt to test it
failed.** `bench external` reads published SWE-agent rollouts
(`SWE-bench/SWE-smith-trajectories`), which look ideal — many attempts per task
by one model, each graded pass/fail. They are not. The same task id is issued
under two different prompts, *fix the source* and *write a bug report*, and the
pass/fail grade comes from the tests, which a bug-report run can never satisfy
because it never edits source. Pairing a pass against a fail therefore compares
two runs asked to do different jobs. Once pairs are required to share an
instruction, that corpus yields **zero** usable pairs: the fix-the-source prompt
was run once per task, so there is nothing to pair.

The loader now refuses cross-instruction pairs rather than producing a
comfortable number from them. Finding a real external set remains open; SWE-bench
leaderboard submissions publish per-instance traces and would work, at the cost
of a reader per submission format and the caveat that pairs there are
cross-model.

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

## Replay as a record

The reason to record at the tool boundary rather than log after the fact is that
a cassette can be *re-derived*. Given a run id you can reconstruct exactly what
the model was shown and what the agent did with it, offline, with the network
switched off — and get the same bytes every time. Cassettes are JSONL, so that
record is reviewable in a pull request rather than trapped in a vendor console.

That is mostly a debugging property, and debugging is what this is for. But it
is also what you need to answer questions after the fact: what did this agent
actually read, which files did it touch, what was in its context when it made
the change that broke something. Agents increasingly run with real credentials
against real repositories, and "we think it did X" is a weaker answer than a
log you can replay. Provenance tags make the second question — *where did that
instruction come from* — answerable too, which is the shape of the problem when
untrusted content reaches an agent's context.

Scoped honestly: this is a recorder and a differ. It has no threat model, spots
nothing on its own, and enforces no policy. Redaction is default-on so cassettes
can be committed without leaking credentials, and that is the whole of its
security posture.

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

## Diffing two runs

```
locus diff <good-run> <bad-run>
```

Each run is turned into a sequence of steps — a tool name, its arguments, the
model's reasoning text for that turn, and the set of source files changed so
far. Parallel tool batches from one model call count as a single step. The two
sequences are aligned with Gotoh's affine-gap algorithm so a multi-step
excursion is charged once rather than per step, and the divergence point is the
first step on the failing side after the traces stop re-aligning.

**The divergence rule needs the two runs to end differently.** It walks backward
to the last column where the runs still agree, so an agreement at the very end
is indistinguishable from real recovery — even a single coincidental one. If a
harness ends every run with the same terminal action, that column always
agrees and every pair reports "no standing divergence." If you point `locus
diff` at a harness with a fixed ending, strip it first. This corpus mostly
didn't show it because it recorded a terminal step only for runs that actually
submitted, but one real case survived anyway: a failing run stuck repeating a
bare `run_tests()` several times pairs against separate, genuine `run_tests()`
calls on the passing side, and because there are several of them the repetition
reads as sustained agreement rather than a stuck loop. A run-length threshold
was tried as a general fix and made the measured accuracy worse, not better —
the repetition itself is the artifact, not how many columns it spans, and a
threshold big enough to catch it also discards real one-column recoveries
elsewhere in the corpus. Left unresolved rather than patched under time
pressure.

Step similarity is a fixed weighted sum: tool name, argument similarity after
path canonicalization, embedding cosine on the reasoning text, and Jaccard
overlap of changed files. The weights were chosen before any hand labels were
scored, so the accuracy number is not a product of tuning on the evaluation set.
Reasoning embeddings use a pinned local model (`mlx-community/bge-small-en-v1.5-bf16`);
install the optional extra with `uv sync --extra embeddings`. `--lexical` skips
the model and scores reasoning text by string similarity instead.

The headline rate is not uniform across the set. On the stricter 3:1 length
subset the aligner is at 21/30 against 7/30 for first-difference; on the 29
contestable pairs it is 20/29 against 0/29. McNemar is 20–0 against
first-difference over all pairs (n_discordant 20) — no baseline won a single
pair anywhere — but with a discordant count that small, read the gap rather
than the p-value.

The constant-guess diagnostic is the reason to be careful with the headline.
Always answering "step 6" scores 32/41, the same as the aligner, and 22/29 on
the contestable subset; the best constant there is step 7 at 23/29. Both beat
the aligner's 20/29. Labels cluster because most failing runs here did not take
a wrong turn — they ran out of budget while still exploring, so the answer is
usually that run's last step, and the median failing trajectory is 6 steps.
Within-±2 on trajectories that short cannot separate a
method from a constant. What the numbers do support is the comparison against
the named alternatives, which is what the baselines are for.

Pre-specified ablations on the same 41 pairs: scoring arguments by target file
or pattern alone matches the full distance (32/41); bag-of-words reasoning
matches BGE (32/41); swapping Gotoh for linear gaps loses three pairs (29/41);
full-argument equality loses two (30/41); dropping the reasoning term entirely
gains one (33/41). So on this corpus the load-bearing piece is target-width
argument similarity, not affine gaps or embeddings — those stay in the default
because the design is aimed at longer excursions than this set mostly contains,
not because they moved the headline here.

## The HTML report

```
$ locus view 16ccfac0 45af6f8f -o report.html
wrote report.html (0.21 MB) — divergence at failing step 9
```

One file, no server, no network. The comparison travels inside the page as JSON,
because a `file://` page cannot read the SQLite store without either a server or
a WASM SQLite build; the report opens from disk, from a README link, or straight
out of a CI artifact.

It shows the two trajectories aligned side by side with the divergence
highlighted, and for whichever step you select, the full context that produced
it — every block labelled with where it came from, whether that is the system
prompt, the bug report, a file the agent read, or test output. Arrow keys walk
the alignment.

Repeated context is stored once. An agent resends its whole history every turn,
so embedding each turn's context separately would grow the payload with the
square of the trajectory length; keyed by content it does not. On the 41 labeled
pairs that is up to a 23× reduction against the raw event log, and the largest
report comes out at 0.3 MB with a median of 0.13 MB. The embedded data is capped
at 5 MB (`--max-bytes`): past the cap, context blocks are clipped to a shared
per-block limit, the page reports how much it dropped, and the full text stays
in the store. No pair in that set reached the cap.

## Token profiles

```
locus pprof <run> --view tokens -o run.pb.gz
locus pprof <run> --view tokens --top 20
```

```
run 16ccfac03ddc  bidict-deleted_guard-3#1
total  input=117033  output=1886
leaf                              input     output    share
file_read                         47095          0   39.6%
assistant_reasoning               30821          0   25.9%
tool_schema                       12825          0   10.8%
test_output                        5801          0    4.9%
tool_output                        5544          0    4.7%
system_prompt                      5394          0    4.5%
error_feedback                     4644          0    3.9%
task_issue                         3285          0    2.8%
```

Two-thirds of everything this run paid for was files it had read and its own
earlier reasoning being handed back to it. That breakdown is only possible
because the provenance of each block was recorded at the time; it cannot be
recovered from a finished transcript.

Token spend is exported as a standard gzipped pprof profile, so Speedscope,
`go tool pprof`, and Pyroscope can open it without a custom renderer. The stack
is run → model → turn → provenance tag. Sample types are input and output
token counts.

Usage is measured per model call. Input tokens are split across that call's
context blocks in proportion to character length (largest-remainder, so the
parts still sum to the measured total). That split is proportional, not a
per-block measurement — say so when you cite it. Output tokens sit on a single
`response` leaf.

On a corpus run of `bidict-deleted_guard-3` (117,033 input + 1,886 output
tokens), the profile's sample totals matched the recorded usage exactly, and
Speedscope imported the file as a protobuf pprof profile.

The same aggregation backs the spend panel in the HTML report, so the profile
and the page cannot drift. There are no dollar figures anywhere: the corpus ran
on a local model, which cost nothing, and an invented price would be worse than
no number.

## Counterfactual re-runs

```
locus intervene <run> --drop-tag file_read --from-step 4 -- <your agent command>
```

Take a recorded run, remove a class of context block from a chosen turn onward,
and let the agent run forward from there into a *new* run. The original is never
written to. Every context block in the HTML report carries the exact command
that neutralizes it — a `file://` page has no server and cannot start a replay,
so the page offers the command rather than a button that could not work.

Two things about how this runs, both of which cost more than they sound like:

**The model replays; the world is re-executed.** Serving a recorded tool result
would skip that call's effect on the working tree, so a run continuing past the
change would act on a tree the replayed prefix never actually built. Inference
is the input the log exists to capture; tool calls are the agent's effect on the
world and have to happen for that world to be real.

**So the free prefix ends at the first tool output that differs.** Re-executing
means a tool whose output is not byte-identical — a test runner that prints its
own duration, for one — changes the next turn's context and stops it matching.
Forking the passing `bidict-deleted_guard-3` run at turn 4 replayed **1 of the 4
prefix turns**, the same on both attempts, because the first test run reported a
different elapsed time than when it was recorded. That is the tool reporting
divergence rather than hiding it, but it means "replay is free up to the
intervention" holds only for agents whose tools are deterministic in their
output text.

For the same reason the fork itself is not reproducible run to run: two forks of
that run with identical arguments took 12 and 19 generated turns, because a test
duration in the prompt is enough to move the trajectory. Both produced a
well-formed patch, and neither made the suite pass — as the source run also did
not.

The sampler is held at the seed the source run used, advanced to the turn the
fork starts generating at, so the model's own sampling is not a second thing
that changed.

Forks are written to a separate store from the runs they came from
(`--source-store`), so a finished corpus can be forked without growing, and
`locus diff --store-b` compares across the two.

## OpenTelemetry

```
locus otel <run> -o trace.json
```

One trace per run — a root span, a `chat` span per model call, an
`execute_tool` span per tool call — written as OTLP/JSON with the OpenTelemetry
GenAI semantic convention attributes (`gen_ai.system`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.tool.name`, and so on). Span ids are
derived from the run and call ids by hash, so exporting a run twice produces the
same trace instead of a new one.

Written directly as OTLP/JSON rather than through the OpenTelemetry SDK, which
keeps the wheel at two dependencies. The tests check the structure, the
attribute names, that the spans form one tree, and that exported usage equals
recorded usage. They do not check that a particular collector ingests it — that
would need a collector running, so treat this as convention-shaped export rather
than a certified integration.

## Prior art

The record/replay design — cassettes, request matchers, record modes, before-
record redaction, staleness intervals — is borrowed from VCR.py and Ruby's VCR,
which solved this for HTTP fifteen years ago. Trajectory alignment uses Gotoh
affine-gap dynamic programming (the same family as Needleman–Wunsch) over a
distance on agent steps rather than residues. The backward divergence rule —
the last position after which two traces never re-align — is the same move as
the point-of-commitment idea in Causal Agent Replay (arXiv 2606.08275). Token
flamegraphs as standard pprof rather than a custom renderer follows
`agentpprof` / AgentSight; what locus adds is provenance tags already in the
event log and (separately) intervention replay over a profiled block. What's
new as a whole is applying record/replay to model calls, tool calls, the
filesystem and the clock, and evaluating trajectory alignment against named
baselines on hand-labeled pairs.

## Development

```
uv sync
PYTHONHASHSEED=0 uv run pytest
```

`tests/test_offline.py` is the load-bearing test: an agent whose model backend
genuinely opens a socket is recorded through the CLI, then replayed. The test
server counts connections, and the replay must reproduce the run byte for byte
with that count at zero.
