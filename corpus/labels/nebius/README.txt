Blind labelling packets from nebius/SWE-agent-trajectories. 70 packets in two
draws that share this directory but are not one set.

  packets/     one markdown file per pair. Do not open key.jsonl
               while labelling.
  key.jsonl    packet_id -> instance / model / dataset rows, plus `batch`.
  labels.jsonl `label` is the 1-based FAILURE step.

TWO BATCHES, AND WHY THEY STAY DISTINGUISHABLE

  batch nebius-1   N01-N40, seed 20260812. The transfer set the rule was
                   selected against. In-sample for anything built afterwards.
  batch nebius-2   H01-H30, seed 20260815. Drawn from the 79 pool pairs that
                   nothing had rendered or scored, labelled from the packets
                   alone before any rule ran against them, scored exactly once.

They were merged into one directory for convenience, not because they are
interchangeable. One was selected against and the other was not, so pooling
their scores would restate a held-out number as an in-sample one. Both
consumers filter on `batch`: `bench.pooled` reports nebius-1 as its nebius
column, `bench.heldout` reports nebius-2 as the held-out slice. Anything new
that reads this directory has to decide which it wants.

SCORED, ONCE EACH

  nebius-1 (40), via bench.nebius.score_packets, 2026-08-15:
    earliest_bound 19/40, first_commitment 15/40, align-v1 4/40.

  nebius-2 (30), after all labels were saved:
    H01-H15 (prior pass), within +/-2: earliest_bound 8/15,
      constant-10 5/15, first_commitment 4/15, align-v1 1/15.
    H16-H30, within +/-2: earliest_bound 10/15, first_commitment 9/15.
    H01-H30, within +/-2: earliest_bound 18/30, first_commitment 13/30;
      within +/-5 21/30; within +/-10 24/30.

Both are spent. Labels were written from the packets alone with no method's
prediction visible, which makes comparisons between rules fair without making
the labels independent of the rules — the same author decided both. See
contracts/divergence.md for what these labels do and do not support.
