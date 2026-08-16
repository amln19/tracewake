Held-out set for the second labelling pass. 140 fresh trajectories, labelled
under PROTOCOL.md and scored exactly once.

  packets/     one markdown file per trajectory, failing side only, with the
               environment's observations included and the source, model and
               instance withheld.
  key.jsonl    packet_id -> instance / model / dataset row.
  labels.jsonl `label` is the 1-based FAILURE step. Append only.

Drawn by bench.relabel.draw_test, seed 20260815, stratified: nebius
swe-agent-llama-405b x25, llama-70b x50, llama-8b x25, OpenHands gpt-4o x40.
One rollout per instance, chosen uniformly -- several rollouts of one bug are
not independent evidence, and selecting on any property of the trajectory
would shift the length distribution that most predicts whether a rule lands.

WHY IT EXISTS

Every earlier labelled set is spent: the rule was chosen while looking at all
four, and both checks on that choice were scored once. Requiring a passing
reference run per failure had held the usable pool to a few hundred pairs; a
single-trace rule needs no reference, and without that constraint the sources
hold roughly 72,000 failing trajectories over 5,800 instances.

ELIGIBILITY

Two filters, both fixed before the draw. A rollout with no model prose in any
turn is not labelled -- it produced nothing rather than reasoning its way into
failure, so there is no point of no return in it. Nor is a rollout the
environment never told anything, where every observation comes back identical
because the tool being called returns an empty listing. Together these exclude
about 38% of OpenHands failing rollouts and none of nebius.

CHECKED BEFORE SCORING

  * the exported key matches a fresh draw_test rerun exactly;
  * zero instance overlap with calibration/, any first-pass set, or RootSE;
  * stratification matches the plan;
  * every label lies within [1, step_count];
  * label position is not clustered at an extreme: 22% at step 1, 4% at the
    last step, median at 49% of the trace;
  * the 2 E2 and 3 E3 exclusions hold up on inspection.

Four packets (T090, T094, T099, T103) carry two label lines each, from an
accidental overlap at a session boundary rather than a deliberate correction.
All four agree on the step; T099 differs on confidence between passes. Both
lines are kept and the last one is authoritative, per the append-only rule.

RESULT

135 integer labels, 2 E2, 3 E3. Scored once, deterministic on rerun, by

  uv run --group bench python -m bench.score_holdout2

E2/E3 items are not scored: there is no location for a rule to land on.

                    exact          +/-2           +/-5
  earliest_bound    51/135 37.8%   76/135 56.3%   87/135 64.4%
  first_commitment  50/135 37.0%   71/135 52.6%   81/135 60.0%

At +/-2 every item here is missable; at +/-5, 121 of 135 are. The full
reading, including where the rule fails and the two defects this evaluation
found in the earlier figures, is in contracts/divergence.md under "The second
evaluation".

The caveat that governs these numbers: the labels are anchored to writes and
the rule reads writes. Labels here sit on a writing step 68-78% of the time
against 42% for RootSE, the only externally labelled set. These figures are
partly a measure of that agreement.
