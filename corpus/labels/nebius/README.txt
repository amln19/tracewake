Blind labelling packets from nebius/SWE-agent-trajectories. 70 packets.

  packets/     one markdown file per pair. Do not open key.jsonl
               while labelling.
  key.jsonl    packet_id -> instance / model / dataset rows, plus `batch`,
               which `bench.relabel` reads to draw calibration items
               proportionally across sources. Not otherwise meaningful.
  labels.jsonl `label` is the 1-based FAILURE step.

Labels were written from the packets alone with no method's prediction
visible, which makes comparisons between rules fair without making the labels
independent of them — the same author decided both. See corpus/labels/README.txt
for what these labels do and do not support, and corpus/alignment/cleanroom-partition.json
for which of these 70 are currently train and which are test.
