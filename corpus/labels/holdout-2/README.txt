Held-out set. 140 fresh trajectories, labelled under the protocol in
../README.txt and scored exactly once.

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

ELIGIBILITY

Two filters, both fixed before the draw. A rollout with no model prose in any
turn is not labelled -- it produced nothing rather than reasoning its way into
failure, so there is no point of no return in it. Nor is a rollout the
environment never told anything, where every observation comes back identical
because the tool being called returns an empty listing. Together these exclude
about 38% of OpenHands failing rollouts and none of nebius.

CHECKED

  * the exported key matches a fresh draw_test rerun exactly;
  * zero instance overlap with calibration/, any other Tracewake-labelled set,
    or RootSE;
  * stratification matches the plan;
  * every label lies within [1, step_count];
  * label position is not clustered at an extreme: 22% at step 1, 4% at the
    last step, median at 49% of the trace;
  * the 2 E2 and 3 E3 exclusions hold up on inspection.

Four packets (T090, T094, T099, T103) carry two label lines each, from an
accidental overlap at a session boundary rather than a deliberate correction.
All four agree on the step; T099 differs on confidence between the two lines.
Both are kept and the last one is authoritative, per the append-only rule.

RESULT

135 integer labels, 2 E2, 3 E3. Scored once, deterministic on rerun, by

  uv run --group bench python -m bench.score_cleanroom

E2/E3 items are not scored: there is no location for a rule to land on. See
contracts/divergence.md for the current figures.

At ±2 every item here is missable; at ±5, 121 of 135 are. See
contracts/divergence.md for the full reading, including where the rule fails.

The caveat that governs these numbers: the labels are anchored to writes and
the rule reads writes. Labels here sit on a writing step 68-78% of the time
against 42% for RootSE, the only externally labelled set. These figures are
partly a measure of that agreement.
