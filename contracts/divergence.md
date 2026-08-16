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

An agent was given 107 labelled development trajectories and none of this
project's earlier rules, documents, evaluation or held-out data, and asked to
build a predictor. It produced this. The rule it replaced had been reached by a
sweep over roughly seventy candidate signals and differs only in how ownership
is decided — by the run's own read history rather than by the action verb.

Two rules reaching the same idea from opposite directions is the strongest
evidence available that the idea is in the data rather than in the method. The
honest qualifier is that the labels are write-anchored, so writes are partly
what they were always going to point at. See "The labels are anchored to
writes".

## Measured

140 fresh trajectories, drawn after the rule was frozen, instance-disjoint from
everything ever labelled, protocol registered before the first label
(`corpus/labels/PROTOCOL.md`, committed before it). 135 carried a label; 5 were
unlabellable. Scored once.

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
was measured, once, on the 178 pairs the first evaluation used: **47/178 = 26%
within two steps, against 96/178 = 54%** for a commitment rule on the same
pairs. That is why a separate rule exists rather than reusing the alignment the
tool already computes. The comparison is recorded here rather than maintained,
because the pairs it used are development data for the current rule and
rerunning it would prove nothing.

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

Sixty trajectories carrying a first-pass label were re-rendered and labelled
again under the registered protocol, without the old label visible. Agreement
was 40.8% exact, 49.0% at ±2 and 69.4% at ±5, against chance floors of 4.1%,
19.7% and 38.0% — far above chance, so the rubric reproduces what the earlier
pass meant.

But a fixed rule scored against the two labellings of the *same* trajectories
gets 31% and 45% exact. **An 18-point swing at exact match and 12 at ±2, from
relabelling alone.** No figure in this document is precise to better than that,
and no comparison separated by less than it is supported by this evidence.

Two further consequences worth stating:

* **A window can be wider than the trace.** At ±2 a trace of five steps or
  fewer cannot be missed by any prediction inside it. Every window figure here
  is reported with its chance rate for that reason; the held-out set is 100%
  missable at ±2 and 90% at ±5.
* **A labeller's confidence does not predict their accuracy.** Items marked
  confident scored *worse* than the rest (28% against 46% exact). Confident
  labels sit at a median 19% through their trace against 71%: the flag recorded
  "this run was obviously doomed early", which is a property of the trace, not
  of the label.

## Data defects found and fixed

* **A fifth of the OpenHands set had no agent in it.** 36% of its failing
  rollouts emit no prose in any assistant turn — a few identical tool calls
  against an empty directory listing, no patch, `empty_generation` from the
  grader. Sixteen of eighty first-pass labels sat on such rollouts, median four
  steps, where a commitment rule reads 94% within ±2 against 55% on the rest,
  purely because the window exceeds the trace. Both filters now run before any
  draw.
* **`(instance_id, run_id)` is not unique in the OpenHands dump.** 197 pairs
  name two rollouts each, so a join on that key silently picks one. It is what
  left E10, E36 and E76 byte-identical on both sides and recorded for months as
  labels that were noise. The labels were fine. Seven packets were affected; the
  loader now breaks ties on which side resolved and how long it was.

## What else was tried and did not work

Roughly seventy candidate signals were measured against a commitment rule on the
178 pairs of the first evaluation, and a further set during the rebuild. All of
these lost. They are recorded so they are not tried again, and they are facts
about the data rather than about any particular rule, which is why they survive
the rule having changed:

* **Terminal repetition as the readout rather than a bound.** It discriminates
  well — it fires on 27/40 nebius failures against 4/40 successes — but fires
  *late*: of 27 nebius answers, 18 were too late and 2 too early, median +7
  steps. The fatal edit precedes the loop by many steps.
* **Novelty decay / `last_novel_action`.** Correlates 0.85 with the label — and
  0.98 with trace *length*. Partial correlation given length is 0.24. It was
  measuring length.
* **Commitment variants**: last, unrevised, solo, median, 2nd, 3rd. All worse
  than the first.
* **Score-based readouts.** Last column above mean similarity, and a
  parameter-free maximum-segment changepoint over the similarity profile. Both
  ≤26% on RootSE.
* **Reference length** as an a priori bound, capping the answer at the length
  of the successful run: net +3 of 178. Noise.
* **CodeTraceBench** (4,316 coding-agent trajectories, human-verified
  step-level annotations, MIT). Assessed and not adopted. Its labels mark which
  steps were *incorrect or unuseful*, not which step made recovery impossible:
  successful runs carry incorrect steps too, a failed run carries three to six
  of them scattered through the trace, and there is no decisive-step flag. It is
  a different question. Adapting it would also need four framework formats,
  with OpenHands trajectories split across roughly 36 per-model-call files whose
  mapping to the annotation's `step_id` is not stated — a silent misalignment
  there would corrupt every label without failing anything.
* **Reasoning-text repetition**, median-of-five ensembles, and per-scaffold
  fallback constants. All worse or within noise.
* **Tail slack on the periodicity test** (letting the cycle end k steps early):
  RootSE firing goes 0→6 of 58 at k=5. A knob for nothing.

### Confidence and output shape were searched too, and are also flat

Beyond the point estimate, two further families were measured on development:

* **Ranking pairs by predicted correctness.** Ten orderings, including trace
  length, the spread between the three bounds, path count, commitment count,
  and combinations. Best area under the risk–coverage curve was 0.667 against
  0.663 for the five published classes, and the published classes were better at
  40% coverage (78% against 67%). Trace length is the strongest single feature
  at 0.63 pooled standard deviations; the *number of bounds that fire* separates
  hit from miss by 0.01, which is nothing.
* **Stability under perturbation** — recomputing the answer under three
  signature notions and three truncations, then using the variance as
  confidence, and the median or mode as the estimate. As an estimator it loses
  (54/113 against 55). As confidence it separates by 0.49 standard deviations
  but does not improve the curve where it matters.
* **Reporting the span between the bounds as a window** rather than a point.
  The span contains the label 67% of the time; a fixed window of the same
  median width centred on the point estimate contains it 76%. So a span between
  bounds was worse than its own midpoint at delimiting a range.

### Observations carry no usable bound

Every signal above reasons about what the run *did* and never about whether it
worked. That looked like the gap: an observation can show a step was wrong
before any write, which is exactly where the residual sits.

`Step.observation` carries it across all three adapters — 96% of RootSE steps,
97% of OpenHands, 98% of nebius. Six ways of using it were measured on the
development halves and all tied or lost: repetition and novelty over
observations changed nothing, first-rejected-edit moved one pair, first repeated
observation and first empty search result lost, and first error in an
observation lost badly, because normal exploration produces errors constantly.

When the action repeats the observation repeats, so observations are largely
redundant with actions, and the extra cases they catch do not fall earlier. The
field is kept because the plumbing is small and one write-detection correction
depends on it. Nothing in `diverge` reads it, and a test pins that.

### RootSE does not loop

Terminal repetition fires on **0 of 58** RootSE failures. Their mean
duplicate-action fraction is 0.14, against 0.53 for nebius and 0.36 for
OpenHands. RootSE's runs come from stronger backbones and end cleanly with
`submit`; thrashing is a weak-model pathology, so every repetition-based signal
is worth nothing on that population and a pooled number hides it.

The stronger statement, established later: repetition is worth nothing on
**every** population as a bound, because periodicity to the end implies novelty
exhaustion no later, so it can never be the tighter of the two. Novelty gets
there first, by construction rather than by measurement.

### Write detection has been corrected three times and never helped

Ownership by verb rather than by novelty took RootSE failures registering no
commitment from 14/58 to 4/58 — ten runs with up to fifteen writing steps each
that the rule previously had nothing to say about — while pooled ±2 moved 89 to
90 of 178. An earlier pass removed 351 invented writes from shell parsing and
left ±2 accuracy identical.

A fourth followed from observations: the nebius adapter counted edits the
editor had rejected, which `bench.rootse` had always filtered. Removing them
drops 13 phantom writes across the development half and changes no answer.

Four correctness fixes, no accuracy. That is itself evidence about how much of
the commitment signal is noise. They are kept because the old behaviour was
wrong, not because they helped.

## Limits

* **Absolute accuracy is low.** 17.6% exact and 45.1% within ±2 on external
  labels, out-of-sample. The claim is that a zero-cost structural method is
  worth having at all, not that it localises long traces well.
* **It is uneven across scaffolds**, and weakest on the one it was developed
  against, so the ordering is not explained by familiarity.
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
  which is what relabelling alone moved a fixed rule's score on 49 identical
  trajectories. Comparisons separated by less than that are not supported by
  this evidence.
* **What remains unreached needs language.** The residual concentrates on runs
  whose point of no return precedes any write — 44% of RootSE — and 18 of
  RootSE's 102 labels sit on a step with no action at all. A structural rule
  reads actions; there is nothing there to read.
