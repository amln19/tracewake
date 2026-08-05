External transfer labels (OpenHands-Sampled-Trajectories).

  packets/     blinded markdown; do not open key.jsonl while labeling.
  key.jsonl    packet_id → instance/run ids.
  labels.jsonl 30/30 filled, single annotator, seed 20260805.

Operational definition matches the synthetic sheet: earliest FAILURE step after
which the run could not plausibly recover; stuck identical-action tails mark
the last step that did something new; empty productive path → last FAILURE step.

Score:  python -m bench external score
Needs:  pip install datasets  (Hugging Face reload of the named runs)
