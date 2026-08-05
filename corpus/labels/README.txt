Blind divergence labels for the alignment evaluation set.

  packets/     one markdown file per pair, anonymous ids, shuffled.
  key.jsonl    maps packet_id -> task/run. Needed to score; not opened during
               labeling, since the task id names the bug.
  labels.jsonl the evaluation labels — complete (41/41), single annotator.

Selection seed 20260730; shuffle seed 20260771.
41 pairs. The operational definition is at the top of every packet, including
the rule for a run that ends by repeating an action it already took.

Label:  python -m bench label
Score:  python -m bench align-eval --score
