Held-out labelling packets from nebius/SWE-agent-trajectories.

  packets/     one markdown file per pair. Do not open key.jsonl while labelling.
  key.jsonl    packet_id -> instance / model / dataset rows.
  labels.jsonl `label` is the 1-based FAILURE step.

30 packets exported, seed 20260815, drawn from the 79 pool pairs that nothing
had rendered or scored. 15 carry a label; the remaining 15 are unlabelled and
stay that way until there is a reason to spend them.

This set exists because the improvement sweep of 2026-08-15 selected on all four
of the sets it then reported. Those figures are in-sample. These pairs were
labelled from the packets alone, before any rule was run against them, and
scored exactly once.

Scored 2026-08-15, within +/-2 of the label:
  earliest_bound 8/15, constant-10 5/15, first_commitment 4/15, align-v1 1/15.
See contracts/divergence.md.
