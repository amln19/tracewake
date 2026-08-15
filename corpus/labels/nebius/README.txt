Blind labelling packets from nebius/SWE-agent-trajectories.

  packets/     one markdown file per pair. Do not open key.jsonl
               while labelling.
  key.jsonl    packet_id -> instance / model / dataset rows.
  labels.jsonl fill `label` with the 1-based FAILURE step.

40 pairs, seed 20260812.

This was the last untouched transfer set; it is now spent. Labels were
written from the packets alone, with no method's prediction visible.

Scored once, 2026-08-15, via bench.nebius.score_packets:
  earliest_bound 19/40, first_commitment 15/40, lexical-v1 4/40.
See contracts/divergence.md for what these labels do and do not support.
