Held-out labelling packets from nebius/SWE-agent-trajectories.

  packets/     one markdown file per pair. Do not open key.jsonl while labelling.
  key.jsonl    packet_id -> instance / model / dataset rows.
  labels.jsonl `label` is the 1-based FAILURE step.

30 packets exported, seed 20260815, drawn from the 79 pool pairs that nothing
had rendered or scored. All 30 are now labelled from their packets alone and
the set is spent.

This set exists because the improvement sweep of 2026-08-15 selected on all four
of the sets it then reported. Those figures are in-sample. These pairs were
labelled from the packets alone, before any rule was run against them, and
scored exactly once.

Scored once after all labels were saved:
  H01-H15 (prior pass), within +/-2: earliest_bound 8/15,
    constant-10 5/15, first_commitment 4/15, align-v1 1/15.
  H16-H30, within +/-2: earliest_bound 10/15, first_commitment 9/15.
    earliest_bound within +/-5: 10/15; within +/-10: 12/15.
  H01-H30, within +/-2: earliest_bound 18/30, first_commitment 13/30.
    earliest_bound within +/-5: 21/30; within +/-10: 24/30.
See contracts/divergence.md.
