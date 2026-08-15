# Second labelling pass — registered before any label exists

Everything the first pass labelled is spent: the rule was chosen while looking
at all four sets, and both checks on that choice were scored once. This pass
draws fresh data and fixes, in advance, what will be measured on it. Written
before the first label; any change after labelling begins invalidates the set.

## Sets

`calibration/` — 60 trajectories that already carry a first-pass label,
re-rendered under this protocol and labelled again without the old label
visible. Agreement says whether these labels mean what the earlier ones did.
Disagreement past a tolerance is a ceiling: no rule can score above the rate at
which two labellers agree.

`holdout-2/` — 140 fresh trajectories, instance-disjoint from everything ever
labelled, stratified over source and model. Scored once, after the rule is
frozen.

Training is everything already labelled: 150 first-pass items and RootSE's 102
external labels. They are contaminated for evaluation and free for development.

## The label

The 1-based index of the earliest step after which no later step could plausibly
have recovered the run, without undoing work already done or outside
intervention. Not the first mistake; not where failure became evident; not the
last step by default. A step with no action can be the answer. Where two
adjacent steps qualify, the earlier. Recoverability is judged against the run's
actual remaining budget.

Exclusions, recorded rather than dropped and never backfilled: `E1` truncated or
malformed, `E2` failed for reasons outside the run's control, `E3` appears to
have solved it, `E4` no judgment reachable. Backfilling would select for
labelable cases.

Each label carries `confident: true|false`, decided when written and never
revised.

## What is measured

**Exact match is the primary figure.** It is what the published methods report,
so it is the only number comparable to them, and it has no degeneracy.

Window accuracy at ±2 and ±5 is reported second, because "how much of the trace
must I read" is the question a debugging tool actually answers. Every window
figure is reported with two things beside it, always:

* **the missable subset** — items where a wrong answer was possible at that
  tolerance. Lead with this.
* **the chance rate** — for a label at position `L` in a trace of `n` steps,
  a uniform random prediction lands within `±k` with probability
  `(min(n, L+k) − max(1, L−k) + 1) / n`. Averaged over items, that is the floor
  the figure stands on.

An item whose chance rate is 1.0 could not have been gotten wrong: at ±2 a trace
of five steps or fewer is unmissable, and 18% of the existing OpenHands set is
in that position, 39% at ±5. Quoting a window figure without its floor is what
made a set of four-step rollouts read as 94% accuracy.

Per-stratum figures (source × model) are secondary and exploratory; the strata
deliberately over-sample the scarce model sizes, so any pooled figure is
reported as a weighted estimate, not a raw average.

## Eligibility

A rollout with no model prose in any turn is not labelled. It has no point of no
return to find: the run produced nothing rather than reasoning its way into
failure. This excludes 36% of OpenHands failing rollouts and none of nebius.
Fixed before the draw.

One rollout per instance, chosen uniformly. Several rollouts of one bug are not
independent evidence, the sources share instance identifiers, and selecting on
any property of the trajectory would shift the length distribution that most
predicts whether a rule lands.

## Order

Calibration is labelled and scored before the held-out set is touched, so a
rubric that turns out not to reproduce the earlier labels can be fixed while the
expensive half is still unspent. The held-out set is labelled before any method
runs against it, in randomised order, and scored once.

Labels are written once. A label later found wrong becomes a recorded erratum,
never a silent edit.

## What this cannot establish

The labeller also has a hand in what "the point of no return" means, so these
labels are not independent of the rules measured against them. That keeps
comparisons between rules fair without making absolute accuracy independent.
RootSE's external labels are the only ones free of this, and they are spent, so
the held-out figure rests on labels produced inside this project. The
calibration set is the only measurement of what that is worth.
