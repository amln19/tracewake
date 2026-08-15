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

## The three bounds

Three facts each bound the point of no return from above:

| Bound | Why it bounds |
| --- | --- |
| `first_commitment` | the run changed something it did not create |
| `terminal_repeat` | its actions became exactly periodic and stayed so to the end, so it is emitting the same actions forever |
| `novelty_exhausted` | past this point it does nothing it does not also repeat |

`earliest_bound` reports the minimum. Taking the minimum of a set of upper
bounds is the only thing to do with them: there is no weight to fit, and no
threshold beyond "at least two whole periods", which is the minimum that makes
a period mean anything.

## Measured

Four labelled sets, 178 pairs, within ±2 steps of the label. Reproduce with
`python -m bench.pooled`.

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

The improvement sweep that produced `earliest_bound` tested about thirty
candidate signals, and from partway through it compared them on all four
labelled sets at once. `earliest_bound` was kept partly because it "never loses
on an individual set", which is a statement about RootSE and nebius. The
reliability classes and their percentages were likewise chosen and computed on
the same 178 pairs they are reported on.

So the table above is descriptive of those pairs, not a prediction about new
ones. Selecting the best of thirty candidates on the data you then report
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
drawn from the untouched remainder, fifteen labelled from the packets alone, and
scored once with the rule frozen:

| Rule | within ±2 | within ±5 | mean abs. error |
| --- | --- | --- | --- |
| `earliest_bound` | **8/15 = 53%** | 11/15 | 3.9 |
| constant 10 | 5/15 = 33% | 9/15 | 5.0 |
| `first_commitment` | 4/15 = 27% | 7/15 | 33.3 |
| `align-v1` | 1/15 = 7% | 4/15 | 39.5 |

53% against 54% in-sample: the headline figure survives contact with fresh data,
which is the one thing the in-sample number could not establish. Fifteen pairs
is a wide interval (27% to 80% bootstrapped), so this bounds the damage rather
than measuring the method precisely.

Two things stand out. `first_commitment` collapses here, 27% against its 51%
in-sample, and its mean error is 33 steps against 3.9 — on this slice the
repetition and novelty bounds are doing nearly all the work, where on the
earlier sets they were worth 3 points. And `silent-long`, the class that was 21%
accurate across the earlier pairs, is 4/4 here. Both are single-digit
observations and neither should be read as a finding.

The remaining fifteen packets are exported but unlabelled, and the other 49 pool
pairs have never been rendered. That is the next honest measurement.

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

About thirty candidate signals were measured against `first_commitment` on 178
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

### RootSE does not loop, and that is a real limit

Terminal repetition fires on **0 of 58** RootSE failures. Their mean
duplicate-action fraction is 0.14, against 0.53 for nebius and 0.36 for
OpenHands. RootSE's runs come from stronger backbones and end cleanly with
`submit`; thrashing is a weak-model pathology. Every repetition-based signal is
worth nothing on that population, and the pooled numbers hide it.

### Write detection has been corrected three times and never helped

Ownership by verb rather than by novelty took RootSE failures registering no
commitment from 14/58 to 4/58 — ten runs with up to fifteen writing steps each
that the rule previously had nothing to say about — while pooled ±2 moved 89 to
90 of 178. An earlier pass removed 351 invented writes from shell parsing and
left ±2 accuracy identical.

Three correctness fixes, no accuracy. That is itself evidence about how much of
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
