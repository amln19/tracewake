# Divergence localization

Where did a failing agent run go irrecoverably wrong?

This is the full evaluation writeup for `tracewake localize`. The repository
README summarises headline results. Score the shipped implementation with
`uv run --group bench python -m bench.score_shipped`.

Tracewake answers this from the failing run alone. No reference run, no
alignment, no inference, no model call. `tracewake localize <run>` reports a
step and how much to trust it.

## Steps

A step is one model call and the tool calls it produced. Parallel tool calls
under a single model call collapse into one step, because a batch is a partial
order and treating completion order as sequence would invent divergence.

Each step carries the paths it alone wrote (`Step.writes`). This is separate
from `changed_files`, which accumulates and so cannot say which step did the
writing.

## The rule

Reading a file is recoverable. Writing one is not, in practice, because these
agents rarely undo. So:

> **The run became unrecoverable at the first step that writes a file which is
> not its own scratch file.**

The scratch file is the first path the run writes that it has never read — the
reproduction script it creates out of nothing. Excluding exactly that one file
is what makes the rule work on more than one scaffold: almost every run writes a
repro early, and counting it as the commitment lands a median of thirteen steps
too early.

A run's steps divide into finding out — reading, searching, running a repro —
and acting on what it thinks it found. The first write to a file that was
already there is the boundary. It turns a diagnosis from a hypothesis into the
premise every later step inherits, and when the diagnosis is wrong what follows
is repair, not reconsideration.

The fallback parameter (`scratch_fallback = 12`) applies to runs that never write
outside their scratch file (approximately one trace in eight). It is stable across
wide ranges and held fixed to prevent tuning on evaluation partitions.

The rule reads no action names, no tool conventions, no observation text, no
reasoning text, and no trace length. It depends solely on: "did this step write a file,
and had that file been examined before".

### Development & Methodology

The rule was derived from analyzing 107 training trajectories (67 Nebius, 40 OpenHands)
to identify structural boundaries between exploratory problem diagnosis and irreversible
commitment. RootSE was withheld entirely during development and evaluated once as a blind,
out-of-sample, externally-annotated benchmark.

## Measured

140 fresh trajectories, instance-disjoint from every other labelled set and
from RootSE, drawn and labelled under a protocol committed before the first
label of the pass existed (`corpus/labels/README.txt`), scored once. 135
carried a label; 5 were unlabellable.

Together with the withheld remainder of the older sets and all of RootSE, the
rule has been scored on **262 trajectories it has never seen**:

| Pool | n | exact | ±2 | ±5 |
| --- | --- | --- | --- | --- |
| SWE-agent | 101 | 32.7% | 51.5% | 62.4% |
| OpenHands | 59 | 44.1% | 59.3% | 69.5% |
| **RootSE** (externally labelled) | 102 | **17.6%** | **45.1%** | **57.8%** |
| **all** | **262** | **29.4%** | **50.8%** | **62.2%** |

Chance rates for the same population are 5%, 22% and 40%.

RootSE is the row that carries the most weight. It is the only set labelled by
people unconnected to this project, and the only figure here that is both
externally labelled and out-of-sample. Score the shipped rule with
`uv run --group bench python -m bench.score_shipped`.

### Against published methods, on their metric

The literature reports exact step match on RootSE:

| Method | Exact match | Cost per instance |
| --- | --- | --- |
| TrajAudit (with reference) | 56.6% | ~122k tokens |
| TrajAudit (without reference) | 50.9% | ~122k tokens |
| All-at-Once prompting | 31.9% | LLM |
| Step-by-Step prompting | 23.3% | LLM |
| **this rule** | **17.6%** | **0** |
| Binary search over steps | 15.8% | LLM rollouts |
| Random attribution | 5.4% | 0 |

A structural method sits between the field's search baselines and its weaker
prompting baselines, at zero marginal cost, and about 39 points behind the state
of the art. No published work reports a purely non-LLM baseline for this task,
which is the gap this fills.

### Alignment vs. Divergence Localization

Alignment answers where two runs diverged in behavior, but does not identify where a
failing run went irrecoverably wrong. Empirically, reading localization off alignment
agreement achieves only 26% within ±2 steps (compared to 54% for the commitment rule
on the same pairs). Consequently, Tracewake decouples alignment matching from divergence
localization.

## Reliability

Two label-free facts — whether the run committed at all, and whether the trace
exceeds 18 steps (the alignment profile's existing long/short split, reused
rather than refitted) — sort runs into five classes whose accuracy ranges widely. Measured
within ±2 on the 262:

| Class | ±2 | n | Band |
| --- | --- | --- | --- |
| `commit-short` | 88% | 69 | high |
| `commit-long-single` | 75% | 20 | moderate |
| `commit-long-many` | 36% | 146 | low |
| `silent-short` | 29% | 7 | low |
| `silent-long` | 10% | 20 | very low |

**Relative reliability ordering remains stable across evaluations.** While exact percentages vary across annotation sets, the confidence bands (`high` through `very low`) provide calibrated operational guidance. In particular, `silent-long` (a long trajectory that never modified pre-existing files) achieves only ~10% accuracy and functions as an explicit signal to abstain from automatic attribution.

`localize` returns the class so callers can make informed abstention decisions; the library avoids silently swallowing uncertain predictions, preserving transparency between "no answer" and "low-confidence answer".

## Where it fails, and the direction is systematic

Accuracy against where the label sits in the trace, on the held-out 135:

| Label position | n | exact | ±2 | Direction of error |
| --- | --- | --- | --- | --- |
| early (<0.33) | 57 | 26% | 42% | predicts **later** than truth 32/57, median +2 |
| mid (0.33–0.66) | 26 | 38% | 69% | earlier 15/26, median −1 |
| late (>0.66) | 52 | 50% | 65% | earlier 25/52, median 0 |

The same shape appears on RootSE: 16% exact on early labels against 19% late,
overshooting 23 of 37 times with a median of +4 steps.

The residual concentrates on runs doomed before they produced anything a write
could anchor to. It is not regression to the middle: a positional baseline —
predict `round(α × len)`, α fitted — scores 5% exact against 27% for a
commitment rule on the same 172 items, confirming the heuristic captures
item-level causal structure rather than a positional artifact.

## Labeling Conventions and Failure Precedence

The primary factor governing divergence localization accuracy is how annotation protocols define failure relative to agent action:

| Set | Never Commits | Commits, Failure Precedes Write |
| --- | --- | --- |
| RootSE (external annotation) | 4% | **44%** |
| SWE-agent (in-house held-out) | 18% | 12% |
| OpenHands (in-house held-out) | 8% | 8% |

In RootSE (annotated by TrajAudit's authors), 44% of failure points precede any workspace write, and 18 of 102 labels sit on turns with no tool action at all (pure reasoning turns). A structural method inspecting tool interactions cannot anticipate a divergence that occurs solely in agent reasoning before an external action is executed.

Conversely, in-house benchmarks emphasize behavioral commitment (the first persistent or destructive action), where ground-truth labels align with file modifications. Consequently, **RootSE serves as the stricter, fully independent out-of-sample benchmark**, establishing an empirical lower bound for structural heuristics.

## Trace length and window boundaries

* **A window can be wider than the trace.** At ±2 a trace of five steps or
  fewer cannot be missed by any prediction inside it. Every window figure here
  is reported with its chance rate for that reason; the held-out set is 100%
  missable at ±2 and 90% at ±5.
* **A labeller's confidence does not track label position the way it might
  seem to.** confident=True labels sit at a median 44% through their
  trace against 33% for confident=False — confident is not simply a
  proxy for "obviously doomed early."

### Observations carry no usable bound

`Step.observation` carries what the environment returned, across all three
adapters — 96% of RootSE steps, 97% of OpenHands, 98% of nebius. When the
action repeats the observation repeats, so observations are largely redundant
with actions, and the cases they catch beyond that do not fall earlier in the
trace than what writes already find. The field is kept because the plumbing is
small and one write-detection correction depends on it. Nothing in `diverge`
reads it, and a test pins that.

## Limits

* **Bounded absolute accuracy**: 17.6% exact match and 45.1% within ±2 on external labels (RootSE). A zero-cost structural heuristic provides an instantaneous, deterministic baseline rather than replacing deep model rollouts across long, ambiguous traces.
* **Uneven scaffold support**: Strongest on structured tool environments; weakest on pure shell environments where file modifications must be inferred from command text (`sed`, shell redirects, patch applications).
* **Missing target metadata**: Scaffolds like AutoCodeRover, whose `write_patch` action omits explicit file paths, cannot be anchored by path-based commitment heuristics.
* **Pre-write cognitive failures**: As observed on RootSE, when an agent's fatal flaw occurs during internal reasoning prior to any workspace modification (44% of RootSE failures), a structural action-based rule has no external artifact to inspect.
