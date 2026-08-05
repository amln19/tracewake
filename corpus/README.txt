What is in here. Only labels/, alignment/, runs.jsonl, tasks.json and this file
are committed — every published number traces back to those, and they are
neither large nor regenerable. The recorded stores are git-excluded.

  store/              the corpus: 192 recorded runs over 64 injected bugs. CLOSED
                      — the agent changed after these were recorded, so appending
                      would mix runs from two different agents under one label.
  runs.jsonl          one line per attempt: labels, trajectory shape, timing.
                      This is what `python -m bench status` reads.
  tasks.json          the 64-bug manifest. Rebuilt with `bench build-tasks`.
  repos/              the sixteen pinned upstream checkouts.
  venv/               the interpreter their test suites run under.
  logs/               batch output from the runs that produced the corpus.
  fidelity/           measurement output, written by `python -m bench divergence`.
                      pairs.jsonl is one line per compared pair. Derived from
                      store/ and rebuildable from it, so it is not precious.
  labels/             blinded divergence-labeling packets for the alignment set.
                      key.jsonl maps packet ids back to runs — do not open it
                      during a labeling pass. Rebuild with
                      `python -m bench export-labels`.
                      labels/external/ is the OpenHands transfer sheet (80
                      packets); fill labels.jsonl then `bench external score`.
                      `bench external export --extend --n N` grows it without
                      disturbing packet ids that already carry a label.
  alignment/          prediction sheets and external_scout.json (source inventory).
  archive-prefix11/   superseded runs from earlier agent versions. Never merge
                      these with store/ — its own README says why.
