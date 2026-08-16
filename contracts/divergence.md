# Divergence localization

Where did a failing agent run go irrecoverably wrong?

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

The only fitted number is a fallback for runs that never write outside their
scratch file, used on about one trace in eight. It is inert: sweeping it from 6
to 20 moves held-out exact match between 29.4% and 29.8%, and parameter-free
replacements score 28.6% and 28.2%. It is kept at its original value rather than
swapped, because choosing between them on the held-out set would be selecting on
the evaluation.

The rule reads no action names, no tool conventions, no observation text, no
reasoning text and no trace length. It needs only "did this step write a file,
and had that file been looked at before".

### Where it came from

An agent was given 107 labelled training trajectories — 67 nebius, 40 openhands
— and asked to build a predictor, with no access to RootSE, to held-out data, or
to any other project document. It produced this rule. RootSE was scored once,
afterwards, and never seen during development; that is what makes it the
externally labelled *and* out-of-sample row in the table below.

The honest qualifier is that the labels are write-anchored, so writes are
partly what they were always going to point at. See "The labels are anchored to
writes".

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

RootSE is the row that carries the most weight and reads the worst. It is the
only set labelled by people unconnected to this project, and the only figure
here that is both externally labelled and out-of-sample. Reproduce with
`uv run --group bench python -m bench.score_cleanroom`.

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

### Why not read it off the alignment

`align-v1` answers a different question — where two runs stopped agreeing — and
`tracewake diff` reports both. Reading localization off the alignment instead
scores far worse: 26% within two steps against 54% for a commitment rule on the
same pairs. That is why a separate rule exists rather than reusing the
alignment the tool already computes.

## Reliability

Two label-free facts — whether the run committed at all, and whether the trace
exceeds 18 steps (`align-v1`'s existing long/short split, reused rather than
refitted) — sort runs into five classes whose accuracy ranges widely. Measured
within ±2 on the 262:

| Class | ±2 | n | Band |
| --- | --- | --- | --- |
| `commit-short` | 88% | 69 | high |
| `commit-long-single` | 75% | 20 | moderate |
| `commit-long-many` | 36% | 146 | low |
| `silent-short` | 29% | 7 | low |
| `silent-long` | 10% | 20 | very low |

**The ordering is what carries.** `commit-short` has measured 87%, 86% and 88%
across three independent evaluations; `silent-long` 21%, 19% and 10%. The
percentages have not survived being re-measured that well, which is why the tool
reports a band and not a number: relabelling the same trajectories moves this
kind of figure by about twelve points, and the two sparse classes swing further
than that on their own.

`silent-long` — a long run that never changed anything pre-existing — should be
read as *cannot localise* rather than as an answer. `localize` returns the class
so the caller can abstain; the library does not abstain on its own, because
hiding the difference between "no answer" and "an answer we distrust" would be
worse than reporting both.

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
commitment rule on the same 172 items, so whatever it is doing is per-item and
not distributional. An attempt to catch the early cases with a non-write signal
(first five actions identical, as a proxy for "never got started") flagged 7 of
172 and their label positions were indistinguishable from the population.

## The labels are anchored to writes, and the target moves with them

How often the point of no return precedes the run's first write:

| Set | never commits | commits, truth before it |
| --- | --- | --- |
| RootSE (labelled by the TrajAudit authors) | 4% | **44%** |
| SWE-agent, first pass (ours) | 27% | 3% |
| SWE-agent, second pass (ours) | 18% | 12% |
| OpenHands, second pass (ours) | 8% | 8% |

A fourteen-fold spread is not a fact about agent runs. The labels land in
different places: ours sit on a step that writes 59% to 78% of the time,
RootSE's 42%, and the TrajAudit annotators put **18 of 102 labels on steps with
no action at all** — a turn where the agent only reasoned — which ours
essentially never do. The controlled case is the two in-house passes over one
pool: 59% then 68%, the higher being the pass whose written standard told the
labeller to find the last sound write and judge it.

That instruction makes labelling consistent and points it at exactly where this
rule looks. So the in-house figures are partly agreement with a labeller aimed
at the rule's own anchor. **RootSE, which the rule scores worst on, is the more
trustworthy signal.**

It also means a "ceiling" on write-anchored rules is not a property of agent
runs: near 56% under RootSE's labelling convention, near 90% under ours. A
ceiling that moves with the instrument is a target partly defined by it. An
uncontaminated number needs a labelling standard written without reference to
what the rule does, and will report lower inter-labeller agreement as the price.

## What the numbers cannot be more precise than

Forty-nine calibration trajectories carrying a first-pass label were
re-rendered and labelled again, without the old label visible. Agreement was
40.8% exact, 49.0% at ±2 and 69.4% at ±5, against chance floors of 4.1%, 19.7%
and 38.0% — far above chance, so the rubric reproduces what the earlier pass
meant.

But the shipped rule scored against the two labellings of the *same*
trajectories gets 26.5% and 44.9% exact (13/49 vs 22/49). **An 18.4-point swing
at exact match, and the same 9-item swing at ±2 (40.8% vs 59.2%), from
relabelling alone.** No figure in this document is precise to better than that,
and no comparison separated by less than it is supported by this evidence.

Two further consequences worth stating:

* **A window can be wider than the trace.** At ±2 a trace of five steps or
  fewer cannot be missed by any prediction inside it. Every window figure here
  is reported with its chance rate for that reason; the held-out set is 100%
  missable at ±2 and 90% at ±5.
* **A labeller's confidence does not track label position the way it might
  seem to.** confident=True labels (n=15) sit at a median 44% through their
  trace against 33% for confident=False (n=34) — confident is not simply a
  proxy for "obviously doomed early." Rule accuracy is 80.0% against
  confident=True and 29.4% against confident=False, but n=15 is thin enough
  that this should be read as suggestive, not load-bearing.

### Observations carry no usable bound

`Step.observation` carries what the environment returned, across all three
adapters — 96% of RootSE steps, 97% of OpenHands, 98% of nebius. When the
action repeats the observation repeats, so observations are largely redundant
with actions, and the cases they catch beyond that do not fall earlier in the
trace than what writes already find. The field is kept because the plumbing is
small and one write-detection correction depends on it. Nothing in `diverge`
reads it, and a test pins that.

## Limits

* **Absolute accuracy is low.** 17.6% exact and 45.1% within ±2 on external
  labels, out-of-sample. The claim is that a zero-cost structural method is
  worth having at all, not that it localises long traces well.
* **It is uneven across scaffolds, and weakest specifically on RootSE** — the
  one external, out-of-sample set, and the one whose labels are least
  write-anchored (see above). That is the leading explanation for the gap, not
  overfitting: the rule never saw RootSE during development.
* **AutoCodeRover cannot be localised by this rule at all.** Its `write_patch`
  action carries no path, so no commitment can be anchored.
* **Shell-only scaffolds are weak.** Where everything goes through bash, writes
  are inferred from redirects, `sed -i`, and patch application.
* **Most of the label sets are Tracewake's own.** The nebius labels in
  particular were written by the same author as the rule, from the trajectories
  alone with no method output visible. That makes comparisons *between* rules
  fair; it does not make the labels independent of them. Absolute percentages
  from that set should not be pooled with RootSE's as if they were the same
  kind of measurement.
* **The labels are anchored to writes and the rule reads writes.** Measured,
  not suspected: our labels sit on a writing step 59–78% of the time against
  42% for the only external set, and two passes over the same nebius pool
  differ by 9 points on that measure according to how the labelling standard
  was worded. Every accuracy figure from a Tracewake-labelled set is partly a
  measure of that agreement.
* **No figure here is precise to better than about 18 points at exact match**,
  which is what relabelling alone moved the shipped rule's score on 49
  identical trajectories. Comparisons separated by less than that are not
  supported by this evidence.
* **What remains unreached needs language.** The residual concentrates on runs
  whose point of no return precedes any write — 44% of RootSE — and 18 of
  RootSE's 102 labels sit on a step with no action at all. A structural rule
  reads actions; there is nothing there to read.
