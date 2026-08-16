Divergence labels written by this project, and the protocol they were written
under. Every set here was labelled blind, in the sense recorded under HOW
"BLIND" IS MEANT below -- which is narrower than the word usually implies, and
the difference matters when quoting these.

RootSE is deliberately absent. Its labels were written by people unconnected
to this project and it is loaded from its own release by bench.rootse, which
is what makes it the only externally labelled row in any score. Of the 262
held-out trajectories the shipped rule is scored against, 102 -- 39% -- carry
these externally written labels and are out-of-sample for that rule; the other
160 carry labels produced inside this project, governed by the in-house caveat
below.

The label definition, exclusion codes, and ordering rules below were committed
before the second labelling pass began, which is what makes them a
pre-registration rather than an after-the-fact description of how labelling
went.

WHAT IS HERE

  labels.jsonl   the original 41-pair alignment set, with key.jsonl and
  key.jsonl      packets/ beside it. Selection seed 20260730, shuffle seed
  packets/       20260771, single annotator, complete 41/41. This is align-v1's
                 evaluation set.

  nebius/        70 labelled, from SWE-agent-trajectories.
  openhands/     80 labelled, from SWE-Gym/OpenHands-Sampled-Trajectories.
  holdout-2/     140 packets, 135 labelled, 5 left null as unlabellable. Fresh
                 trajectories, instance-disjoint from every other set here and
                 from RootSE, labelled before any rule ran against them.
  calibration/   60 packets, 49 labelled, 11 excluded. Re-labels of items that
                 already carried a label from nebius/ or openhands/. Measures
                 agreement, not accuracy; it is the reason AGREEMENT, MEASURED
                 has numbers.

Split roles live in corpus/alignment/cleanroom-partition.json, never in these
directory names. A directory records how a label was made, which never
changes; the partition records what it is currently used for. Encoding the
second in a path would make a repartition look like data moving in the diff.

THE LABEL

The 1-based index of the earliest step after which no later step could
plausibly have recovered the run, without undoing work already done or outside
intervention. Not the first mistake; not where failure became evident; not the
last step by default. A step with no action can be the answer. Where two
adjacent steps qualify, the earlier. Recoverability is judged against the run's
actual remaining budget.

Exclusions, recorded rather than dropped and never backfilled. Backfilling
would select for labelable cases.

  E1  truncated or malformed
  E2  failed for reasons outside the run's control
  E3  appears to have solved it
  E4  no judgment reachable

Each label carries `confident: true|false`, decided when written and never
revised.

ELIGIBILITY

A rollout with no model prose in any turn is not labelled. It has no point of
no return to find: the run produced nothing rather than reasoning its way into
failure. This excludes 36% of OpenHands failing rollouts and none of nebius.
Fixed before the draw.

One rollout per instance, chosen uniformly. Several rollouts of one bug are not
independent evidence, the sources share instance identifiers, and selecting on
any property of the trajectory would shift the length distribution that most
predicts whether a rule lands.

ORDER

Calibration is labelled and scored before the held-out set is touched, so a
rubric that turns out not to reproduce the earlier labels can be fixed while
the expensive half is still unspent. The held-out set is labelled before any
method runs against it, in randomised order, and scored once.

Labels are written once. A label later found wrong becomes a recorded erratum,
never a silent edit -- a superseding line, with the original left readable.

WHAT IS MEASURED

Exact match is the primary figure. It is what the published methods report, so
it is the only number comparable to them, and it has no degeneracy.

Window accuracy at +/-2 and +/-5 is reported second, because "how much of the
trace must I read" is the question a debugging tool actually answers. Every
window figure is reported with two things beside it, always:

  the missable subset  items where a wrong answer was possible at that
                       tolerance. Lead with this.
  the chance rate      for a label at position L in a trace of n steps, a
                       uniform random prediction lands within +/-k with
                       probability (min(n, L+k) - max(1, L-k) + 1) / n.
                       Averaged over items, that is the floor the figure
                       stands on. Implemented as bench.score_cleanroom.chance.

An item whose chance rate is 1.0 could not have been gotten wrong: at +/-2 a
trace of five steps or fewer is unmissable, and 18% of the existing OpenHands
set is in that position, 39% at +/-5. Quoting a window figure without its floor
is what made a set of four-step rollouts read as 94% accuracy.

Per-stratum figures (source x model) are secondary and exploratory; the strata
deliberately over-sample the scarce model sizes, so any pooled figure is
reported as a weighted estimate, not a raw average.

HOW "BLIND" IS MEANT

  What is true.  Packets carry anonymous shuffled ids, show the failing side
    only, and state the operational definition at the top. key.jsonl is not
    opened while labelling. No rule's prediction was visible for any label in
    this tree, and holdout-2 was labelled before any rule ran against it.

  What is not.  Single annotator throughout, and the same author who developed
    the rule. Blind to the predictions is not independent of the rule. These
    labels cannot establish that the target is objectively right, only that it
    was applied consistently and without seeing an answer. The labeller also
    has a hand in what "the point of no return" means, which is why absolute
    accuracy is not independent even though comparisons between rules are fair.

  Do not describe these labels as reliable on the strength of the protocol.
  The protocol earns consistency. The number below is what earns trust, and it
  is lower than people expect.

AGREEMENT, MEASURED

  49 calibration items relabelled without their earlier label visible, scored
  against it:

    exact       20/49  40.8%
    within +/-2 24/49  49.0%
    within +/-5 34/49  69.4%
    median disagreement 3 steps

  This is a ceiling. No rule can be shown to beat the rate at which the label
  set agrees with itself, so it is the scale the headline figures belong on:

    metric        rule    ceiling
    exact         29.4%   40.8%     headroom remains
    within +/-2   50.8%   49.0%     saturated; the rule agrees with the
                                    earlier labels about as often as the two
                                    labelling passes agree with each other
    within +/-5   62.2%   69.4%     close

  Read the +/-2 row carefully before quoting it. It does not mean the rule is
  as good as a person. It means +/-2 has stopped discriminating on this label
  set, because two passes over the same trajectories disagree about as much as
  the rule disagrees with either. A higher +/-2 number would be measuring the
  labels, not the method.

  It is intra-annotator, n=49, and the interval is wide. It bounds how
  reproducible this labelling procedure is, not how correct it is.
