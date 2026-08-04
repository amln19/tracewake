What is in here (all git-excluded).

  store/              the corpus: 192 recorded runs over 64 injected bugs. CLOSED
                      — the agent changed after these were recorded, so do not
                      append. See DECISIONS.md "The corpus is closed".
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
  archive-prefix11/   superseded runs from earlier agent versions. Never merge
                      these with store/ — its own README says why.
