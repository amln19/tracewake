# Divergence localization

Where did a failing agent run go irrecoverably wrong?

Tracewake answers this from the failing run alone. No reference run, no
alignment, no inference, no model call. `tracewake localize <run>` reports a
step and how much to trust it.

`align-v1` — the frozen alignment profile — answers a different question,
namely where two runs stopped agreeing, and is much weaker at this one. It is
kept for compatibility and for visualising an alignment, not for localization.

## Steps

A step is one model call and the tool calls it produced. Parallel tool calls
under a single model call collapse into one step, because a batch is a partial
order and treating completion order as sequence would invent divergence.

Each step carries the paths it alone wrote (`Step.writes`). This is separate
from `changed_files`, which accumulates and so cannot say which step did the
writing.

## Commitment

Reading a file is recoverable. Writing one is not, in practice, because these
agents rarely undo. A step **commits** when it writes a path the run did not
itself bring into existence.

Ownership is claimed by the *action*, not by novelty. `create reproduce.py`
brings a file into being; `sed -i` and `str_replace` presuppose one, whatever
the run has bothered to open first. A run that creates a scratch script and
edits it ten times has committed to nothing.

## The two bounds

Two facts each bound the point of no return from above:

| Bound | Why it bounds |
| --- | --- |
| `first_commitment` | the run changed something it did not create |
| `novelty_exhausted` | past this point it does nothing it does not also repeat |

`earliest_bound` reports the minimum. Taking the minimum of a set of upper
bounds is the only thing to do with them: there is no weight to fit.

### `terminal_repeat` was a third bound and cannot be one

It said: the actions became exactly periodic and stayed so to the end, so the
start of that cycle bounds the point of no return. That is true and useless.
If the actions are exactly periodic from step `k` with at least two whole
periods, every action at or after `k` occurs at least twice, so none of them is
globally unique, so the last globally unique action lies before `k` and
`novelty_exhausted <= k`. **Novelty dominates repetition by construction; the
repetition bound can never be the strict minimum.**

Measured over the 38 traces in this project's labelled data where it fires, it
was never once smaller than novelty. Removing it changed no prediction on any
of the 307 labelled trajectories available, and could not have.

This supersedes the claim below under "RootSE does not loop" that the
repetition bound "buys its margin on the populations where agents thrash". It
buys nothing on any population. The margin `earliest_bound` holds over
`first_commitment` is entirely `novelty_exhausted`.

The function is kept in `tracewake.diverge` as a diagnostic — it describes a
real property of a run more directly than novelty does — but nothing reads it.

## Measured

> **Superseded in part.** A second evaluation, described under "The second
> evaluation" below, found two defects in the figures in this section and
> reports a fresh held-out number against labels drawn and written after the
> rule was frozen. Roughly a fifth of the OpenHands column here is scored on
> trajectories where the model produced nothing at all, and a further slice is
> scored at a tolerance wider than the trace. Read this section with that
> section.

Four labelled sets, 178 pairs, within ±2 steps of the label. Reproduce with
`uv run --group bench python -m bench.pooled`.

| Rule | OpenHands dev | OpenHands held-out | RootSE | nebius | pooled |
| --- | --- | --- | --- | --- | --- |
| `earliest_bound` | 25/40 | 25/40 | 27/58 | 19/40 | **96/178 = 54%** |
| `first_commitment` | 25/40 | 23/40 | 27/58 | 15/40 | 90/178 = 51% |
| `align-v1` | 18/40 | 18/40 | 5/58 | 4/40 | 45/178 = 25% |
| constant 10, fitted on development data | 22/40 | 21/40 | 9/58 | 5/40 | 57/178 = 32% |

Against `first_commitment` alone, `earliest_bound` gains 8 and loses 2
(McNemar p=0.11) and never loses on an individual set. The gain is real but not
separated at this size; the reason to prefer it is that it costs nothing and is
bounded by construction rather than fitted.

The four sets are not equivalent evidence. **RootSE is the only one labelled by
other people** — 58 failures annotated by the TrajAudit authors with the
earliest decisive error step. The OpenHands and nebius labels are Tracewake's
own. Read the columns, not the pooled total.

### Against published methods, on their metric

The literature reports *exact* step match, not ±2. On RootSE:

| Tolerance | `earliest_bound` |
| --- | --- |
| exact match | 12/58 = 21% |
| ±1 | 21/58 = 36% |
| ±2 | 27/58 = 47% |
| ±5 | 34/58 = 59% |

Placed against TrajAudit's published numbers on the same benchmark:

| Method | Exact match | Cost per instance |
| --- | --- | --- |
| TrajAudit (with reference) | 56.6% | ~122k tokens |
| TrajAudit (without reference) | 50.9% | ~122k tokens |
| All-at-Once prompting | 31.9% | LLM |
| Step-by-Step prompting | 23.3% | LLM |
| **`earliest_bound`** | **21%** | **0** |
| Binary search over steps | 15.8% | LLM rollouts |
| Random attribution | 5.4% | 0 |

The honest reading: a structural method sits between the field's search
baselines and its weaker prompting baselines, at zero marginal cost, and is
about 35 points behind the state of the art. No published work reports a
purely non-LLM baseline for this task, which is the gap this fills.

## The earlier figures are in-sample

The improvement sweep that produced `earliest_bound` tested roughly seventy
candidate signals, and from partway through it compared them on all four
labelled sets at once. `earliest_bound` was kept partly because it "never loses
on an individual set", which is a statement about RootSE and nebius. The
reliability classes and their percentages were likewise chosen and computed on
the same 178 pairs they are reported on.

So the table above is descriptive of those pairs, not a prediction about new
ones. Selecting the best of seventy candidates on the data you then report
inflates the winner's margin, and at 6 pairs out of 178 with p=0.11 the gap
between `earliest_bound` and `first_commitment` is not demonstrated at all. The
honest claim for `earliest_bound` is that it is never worse, is a bound rather
than a fit, and costs nothing.

### The 44 external labels that were never used

RootSE annotates 102 failing runs. Only 58 ship a passing reference, so
`load_pairs` returned those and the other 44 went unused — not because they are
worse, but because a reference-based rule cannot attempt them. A single-trace
rule can. `load_failures` returns all 102.

Those 44 are the best held-out set this project has: the labels were written by
the TrajAudit authors, and nothing here had scored them.

| Slice | `earliest_bound` | `first_commitment` | constant 10 |
| --- | --- | --- | --- |
| all 102 annotated failures | 48/102 = 47% | 48/102 = 47% | 17/102 = 17% |
| the 58 used for selection | 27/58 = 47% | 27/58 = 47% | 9/58 = 16% |
| **the 44 never scored** | **21/44 = 48%** | 21/44 = 48% | 8/44 = 18% |

48% held out against 47% in-sample, with a 34–61% interval. Together with the
fresh nebius slice below, that is two independent checks saying the reported
figure is not a selection artefact.

Note that `earliest_bound` and `first_commitment` are identical on every RootSE
slice. The repetition and novelty bounds never fire here, which is the same fact
recorded under "RootSE does not loop": those bounds buy their margin on the
populations where agents thrash, and nothing at all where they do not.

### Scored once on data nothing had seen

The nebius pool holds 119 pairs and only 40 were ever exported. Thirty were
drawn from the untouched remainder, labelled from the packets alone before any
rule was run against them, and scored once with the rule frozen:

| Rule | within ±2 | within ±5 | within ±10 |
| --- | --- | --- | --- |
| `earliest_bound` | **18/30 = 60%** | 21/30 = 70% | 24/30 = 80% |
| `first_commitment` | 13/30 = 43% | | |

**60% against 54% in-sample.** The held-out figure is higher than the one it
checks, which is the opposite of what selection would produce, and settles the
question the in-sample number could not: the reported accuracy is not an
artefact of keeping the best of seventy candidates. Thirty pairs put it between
43% and 77% bootstrapped.

The two halves were labelled and scored separately, fifteen at a time: 8/15
then 10/15 for `earliest_bound`, 4/15 then 9/15 for `first_commitment`. The
swing on the second rule is a reminder of how little fifteen pairs settles, and
why the first slice alone was not treated as a result.

`earliest_bound` leads `first_commitment` by five pairs here against two in 113
on development. On long SWE-agent traces the repetition and novelty bounds carry
much more than they do elsewhere, which is consistent with them being inert on
RootSE and worth 3 points pooled.

The other 49 pool pairs have never been rendered.

## The second evaluation

Every set above is spent: the rule was chosen while looking at all four, and
both checks on that choice were scored once. A second pass drew fresh data,
registered its protocol before labelling anything
(`corpus/labels/PROTOCOL.md`, committed before the first label), and scored
once. Requiring a passing reference run per failure is what had held the pool
to a few hundred pairs; a single-trace rule needs no reference, and without
that constraint the sources hold roughly 72,000 failing trajectories over
5,800 instances. Labels, not trajectories, are the binding constraint.

140 trajectories, instance-disjoint from every set above and from RootSE,
stratified over source and model size. 135 carried a label; 5 were excluded as
unlabellable. `uv run --group bench python -m bench.score_holdout2`, run once.

| Rule | exact | ±2 | ±5 |
| --- | --- | --- | --- |
| `earliest_bound` | **51/135 = 37.8%** | 76/135 = 56.3% | 87/135 = 64.4% |
| `first_commitment` | 50/135 = 37.0% | 71/135 = 52.6% | 81/135 = 60.0% |

Reliability classes hold their order on data none of it was fitted to:
`commit-short` 86% (n=49), `commit-long-many` 36% (n=58), `silent-long` 19%
(n=16). Model size shows no clean effect — nebius 405b 52% and 8b 60% at ±2 —
though per-stratum n is 23 to 50, so read that as no evidence of a strong
effect rather than evidence of none.

### Where it fails: early truths, and the direction is systematic

Accuracy against where the label sits in the trace, on the 135 held-out items:

| label position | n | exact | ±2 | direction of error |
| --- | --- | --- | --- | --- |
| early (<0.33) | 57 | 26% | 42% | predicts **later** than truth 32/57, median +2 |
| mid (0.33–0.66) | 26 | 38% | 69% | earlier 15/26, median −1 |
| late (>0.66) | 52 | 50% | 65% | earlier 25/52, median 0 |

The same shape appears on RootSE: 16% exact on early labels against 19% late,
overshooting 23 of 37 times with a median of +4 steps.

This is an upper-bound rule behaving as one. Both bounds assert "no later than
X"; when the truth is step 1 of a forty-step flail, both land too late and the
minimum of two too-late bounds is still too late. The residual concentrates on
runs doomed before they produced anything for a bound to attach to.

It is not regression to the middle. A positional baseline — predict
`round(α × len)`, with α swept and fitted on training — scores 5% exact and
25% at ±2 against `earliest_bound`'s 27% and 49% on the same 172 items. The
rule beats a fitted constant fraction by 22 points; whatever it is doing, it
is per-item and not distributional.

An attempt to catch the early cases with a non-write signal (first five actions
identical, as a proxy for "never got started") flagged 7 of 172 training runs,
whose label positions were indistinguishable from the population and none of
which had label 1. Nothing there.

### Two defects in the first evaluation, found by the second

**A fifth of the OpenHands set has no agent in it.** 36% of OpenHands failing
rollouts emit no prose in any assistant turn: a few identical tool calls
against an empty directory listing, no patch, `empty_generation` from the
grader. 16 of the 80 OpenHands labels sit on such rollouts, median 4 steps.
`earliest_bound` scores 94% within ±2 on those 16 against 55% on the other 64.

That 94% is not a fact about the rule. **At ±2, a trace of five steps or fewer
cannot be missed by any prediction inside it** — the window is wider than the
trace. 18% of the OpenHands set is unmissable at ±2 and 39% at ±5. The 62%
this document reports for OpenHands held-out is about seven points of trace
too short to be wrong on.

Every window figure in the second evaluation therefore ships with its missable
subset and its chance rate. For a label at position `L` in a trace of `n`
steps, a uniform random prediction lands within `±k` with probability
`(min(n, L+k) − max(1, L−k) + 1) / n`. The second set is 100% missable at ±2
and 90% at ±5, against 82% and 61% for the OpenHands set here.

**Relabelling moves the rule's own score by more than the gaps this document
treats as meaningful.** 60 trajectories carrying a first-pass label were
re-rendered and labelled again under the registered protocol without the old
label visible. Inter-labeller agreement: 40.8% exact, 49.0% at ±2, 69.4% at
±5, all far above their chance floors — the rubric reproduces what the first
pass meant. But `earliest_bound` scored on those same 49 comparable
trajectories gets 31% exact against the first-pass labels and 45% against the
second: **an 18-point swing at exact match and 12 at ±2, from relabelling
alone.** No accuracy figure in this document should be read as precise to
better than that.

### The labels are anchored to writes, and the target moves with them

How often the point of no return precedes the run's first write:

| Set | never commits | commits, truth before it |
| --- | --- | --- |
| RootSE (labelled by the TrajAudit authors) | 4% | **44%** |
| nebius, first pass (ours) | 27% | 3% |
| nebius, second pass (ours) | 18% | 12% |
| OpenHands, second pass (ours) | 8% | 8% |

A fourteen-fold spread is not a fact about agent runs. The labels land in
different places: ours sit on a step that writes 59% to 78% of the time,
RootSE's 42%, and the TrajAudit annotators put **18 of 102 labels on steps
with no action at all** — a turn where the agent only reasoned — which ours
essentially never do. The controlled case is the two nebius passes: same pool,
59% then 68%, the higher being the pass whose written standard told the
labeller to find the last sound write and judge it.

That instruction makes labelling consistent and points it at exactly where
`earliest_bound` looks. So **37.8% and 56.3% are agreement with a labeller
aimed at the rule's own anchor**, not accuracy at locating the point of no
return. RootSE, the only externally labelled set here, remains the set the
rule scores worst on, and that is the more trustworthy signal.

The ceiling in the next section moves with the convention too: near 56% under
RootSE's labelling, near 90% under ours. A ceiling that moves with the
instrument is a target partly defined by it. An uncontaminated number needs a
labelling standard written without reference to what the rule does — and will
report lower inter-labeller agreement as the price.

## The ceiling on a write-based rule

A bound built on writes cannot report a step earlier than the run's first write.
Where the label precedes that write, the rule is wrong by construction, and no
amount of tuning changes it. Measured on the development halves:

| Set | Label precedes the first write | Never writes | Reachable ceiling |
| --- | --- | --- | --- |
| OpenHands dev | 3/40 | 14/40 | 33/40 = 82% |
| RootSE dev | **21/53**, median gap 9 steps | 4/53 | 28/53 = 53% |
| nebius dev | 0/20 | 6/20 | 14/20 = 70% |
| pooled | | | 75/113 = 66% |

`earliest_bound` scores 55/113 on those same halves, so it already captures 73%
of what is reachable. The remaining headroom is 17 points, and over half the
missing mass sits behind labels a write-based rule cannot see at all.

RootSE is where this bites, and the ceiling above is stated too strongly: only
the commitment bound depends on writes. `terminal_repeat` and
`novelty_exhausted` can fire at any step. They simply do not fire usefully on a
population that neither loops nor exhausts its vocabulary.

Characterising the 21 development cases the commitment bound cannot reach: the
label sits at a median step 13, the first write at 26, in traces of 52. The
prefix is `view`, `cd`, `grep` and `find` -- reading and navigating.

One property of those cases is measurable and surprising. **Eight of the 21 sit
on a step with no action at all**, a turn where the agent only reasoned.
Reasoning-only steps are 2.9% of RootSE steps but carry 17% of its labels, an
enrichment of 5.8. The point of no return really does concentrate where the
agent stops acting and thinks.

That did not convert into accuracy. As a bound the signal is inert, because most
labels are still not on such a step. Snapping the answer to a nearby
reasoning-only step is worth one pair in 113 and needs a fitted radius, which is
the kind of knob this file exists to refuse.

So the limit is sharper than "the actions do not carry it". The residual errors
concentrate on steps whose entire content is prose. Reaching them means reading
language, which is a different modality rather than a tighter threshold, and is
what the LLM methods in the comparison above spend their tokens on.

## How much of the trace you have to read

Reporting a single step and scoring it at ±2 understates what the rule
delivers. The useful question for someone debugging is how large a window they
must read to be reasonably sure the answer is inside it. Scored once on the
held-out halves (109 pairs; development figures alongside):

| Window | Steps to read | Development | **Held out** |
| --- | --- | --- | --- |
| ±0 (exact) | 1 | 27% | 27% |
| ±2 | 5 | 49% | **57%** |
| ±5 | 11 | 63% | **71%** |
| ±10 | 21 | 76% | **84%** |
| ±15 | 31 | 80% | 91% |

Median trace is 40 steps, so ±5 is 11 steps, about a quarter of the trace, and
contains the answer 71% of the time.

Restricting to the three classes `reliability` flags as trustworthy, which is
41% of pairs:

| Window | Development | **Held out** |
| --- | --- | --- |
| ±2 | 79% | **37/45 = 82%** |
| ±5 | 92% | 39/45 = 87% |
| ±10 | 97% | 41/45 = 91% |

That is the sharpest honest statement of what this does: **on the two-fifths of
failures it identifies as tractable, an eleven-step window contains the point of
no return 87% of the time, in traces averaging forty steps, at no marginal
cost.**

Held-out ±2 overall is 62/109 = 57% (95% CI 48–66%), against 49% on
development. The held-out half scoring higher than development is the opposite
of overfitting, and comes from its different mix rather than from any tuning.
Per set: OpenHands 62%, RootSE 57%, nebius 45%.

## Reliability

The largest effect is not a better rule but knowing when the rule works. Two
label-free facts — whether the run committed at all, and whether the trace
exceeds 18 steps (`align-v1`'s existing split, reused not refitted) — sort
pairs into classes whose accuracy ranges from 87% to 21%:

| Class | OH dev | OH held-out | RootSE | nebius | pooled |
| --- | --- | --- | --- | --- | --- |
| `commit-short` | 6/9 | 10/10 | 6/7 | 4/4 | **26/30 = 87%** |
| `silent-short` | 10/11 | 8/10 | 0/1 | – | 18/22 = 82% |
| `commit-long-single` | 3/4 | 2/4 | 7/11 | 4/4 | 16/23 = 70% |
| `commit-long-many` | 6/13 | 5/15 | 14/36 | 7/20 | 32/84 = 38% |
| `silent-long` | 0/3 | 0/1 | 0/3 | 4/12 | **4/19 = 21%** |

Abstaining from the bottom classes buys accuracy:

| Answering | Coverage | Accuracy |
| --- | --- | --- |
| all classes | 100% | 54% |
| drop `silent-long` | 89% | 58% |
| … also drop `commit-long-many` | 42% | 80% |
| `commit-short` only | 17% | 87% |

`silent-long` — a long run that never changed anything pre-existing — should be
read as *cannot localise* rather than as an answer. `localize()` returns the
class so the caller can abstain; the library does not abstain on its own,
because hiding the difference between "no answer" and "an answer we distrust"
would be worse than reporting both.

The ordering is monotone in the pool, and the two well-populated extremes
(`commit-long-many`, n=84, and `silent-long`, n=19) separate inside every set.
The middle rows rest on single digits per set and should not be read finely.

## Withdrawn: the reference-based variant

An earlier profile, `commit-v1`, kept `align-v1`'s alignment and reported the
first commitment the *successful* run did not also make. It was withdrawn.

| Set | Reference model | `commit-v1` | single-trace rule |
| --- | --- | --- | --- |
| OpenHands dev | same as the failure | **29/40** | 25/40 |
| OpenHands held-out | same as the failure | **26/40** | 25/40 |
| RootSE | different from the failure | 21/58 | **27/58** |
| nebius | same as the failure | 10/40 | **19/40** |

It wins only on OpenHands — the scaffold it was developed against — and loses
badly everywhere else, because the alignment excuses genuine commitments and
invents differences that are only model idiom.

The first two rows once suggested the comparison paid off when both runs came
from the same model. That explanation was **registered as a prediction before
the nebius labels existed**: those pairs are same-model by construction, so
`commit-v1` should have won. It lost, 10/40 against 16/40. The prediction is
falsified, and with it the case for the reference comparison in general.

Keeping a second profile that wins only on its home scaffold was not worth the
complexity, so it is gone rather than maintained alongside.

## What else was tried and did not work

Roughly seventy candidate signals were measured against `first_commitment` on 178
pairs. All of these lost, and are recorded so they are not tried again:

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
* **Reference length** as an a priori bound, `min(first_commitment, len(good))`:
  net +3 of 178. Noise.
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
  median width centred on `earliest_bound` contains it 76%. So the bounds are
  worse than their own midpoint at delimiting a range, and the window framing
  above uses the point estimate instead.

### Observations carry no usable bound

Every adapter discarded what the environment said back, so each signal above
reasons about what the run *did* and never about whether it worked. That looked
like the gap: an observation can show a step was wrong before any write, which
is exactly where the write-based ceiling below bites.

`Step.observation` now carries it across all three adapters: 96% of RootSE
steps, 97% of OpenHands, 98% of nebius. Measured on the development halves, six
ways of using it all tie or lose against `earliest_bound` at 55/113:

| Bound added to `earliest_bound` | DEV |
| --- | --- |
| terminal repetition over observations | 55/113, unchanged |
| novelty exhaustion over observations | 55/113, unchanged |
| first rejected edit | 55/113 (RootSE +1, nebius −1) |
| first repeated observation | 46/113 |
| first empty search result | 52/113 |
| first error in an observation | 32/113 |

The repetition and novelty variants never produce a tighter bound than the
action-based ones, which says observations here are largely redundant with
actions: when the action repeats the observation repeats, and the extra cases
the observation catches do not fall earlier. Errors are worse than useless,
because normal exploration produces them constantly.

The field is kept because the plumbing is small, the rejected-edit correction
below depends on it, and this result rules out only one use of it -- as an upper
bound combined by minimum. Nothing in `diverge` reads it, and a test pins that.

### RootSE does not loop, and that is a real limit

Terminal repetition fires on **0 of 58** RootSE failures. Their mean
duplicate-action fraction is 0.14, against 0.53 for nebius and 0.36 for
OpenHands. RootSE's runs come from stronger backbones and end cleanly with
`submit`; thrashing is a weak-model pathology. Every repetition-based signal is
worth nothing on that population, and the pooled numbers hide it.

The stronger statement, added later: the repetition *bound* is worth nothing on
**every** population, because novelty dominates it by construction. See "The
two bounds". What the observation here got right is that RootSE does not loop;
what it got wrong is the implication that looping populations are where the
repetition bound earns something. They are not. Novelty gets there first.

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

* **Absolute accuracy is low.** 21% exact, 47% within ±2 on external labels.
  The claim is that a structural method degrades gracefully where `align-v1`
  collapses, not that it localises long traces well.
* **It is uneven across scaffolds**, and weakest on the one it was developed
  against, so the ordering is not explained by familiarity.
* **AutoCodeRover cannot be localised by this rule at all.** Its `write_patch`
  action carries no path, so no commitment can be anchored.
* **Shell-only scaffolds are weak.** Where everything goes through bash, writes
  are inferred from redirects, `sed -i`, and patch application.
* **Two of the four label sets are Tracewake's own.** The nebius labels in
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
  which is what relabelling alone moved the rule's own score on 49 identical
  trajectories. Comparisons between rules separated by less than that are not
  supported by this evidence, including `earliest_bound` over
  `first_commitment`.
* **What remains unreached needs language.** The residual concentrates on runs
  whose point of no return precedes any write — 44% of RootSE — and 18 of
  RootSE's 102 labels sit on a step with no action at all. A structural rule
  reads actions; there is nothing there to read.
