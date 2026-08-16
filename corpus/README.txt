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
  labels/             blinded divergence-labelling packets, one directory per
                      source. Every set has the same shape: packets/ to read,
                      key.jsonl mapping packet ids back to runs (do not open
                      during a labelling pass), and labels.jsonl holding
                      1-based FAILURE step indices, append only.
                        packets/          the original alignment-v1 evaluation
                                          set, 41 pairs.
                        openhands/        OpenHands transfer sheet, 80 packets.
                                          Roughly a fifth of these sit on
                                          rollouts where the model emitted
                                          nothing at all; see
                                          contracts/divergence.md.
                        nebius/           SWE-agent, 70 packets.
                        calibration/      60 items, re-labelled to measure
                                          agreement between two labelling
                                          passes. See its own README and
                                          corpus/labels/README.txt for what the
                                          agreement figure is and what it
                                          bounds.
                        holdout-2/        140 fresh trajectories, instance
                                          disjoint from every other set here
                                          and from RootSE. Scored once by
                                          `bench.score_cleanroom`.
                      corpus/labels/README.txt is the entry point: the label
                      definition, the exclusion codes, the labelling protocol,
                      and the calibration-measured agreement ceiling that
                      every accuracy figure in this project should be read
                      against.
  alignment/          prediction sheets and external_scout.json (source
                      inventory). cleanroom-partition.json is the train/test
                      split, read by bench.score_cleanroom; a directory under
                      labels/ records how a set was labelled, never its split
                      role, so repartitioning is a one-line edit here rather
                      than a move of labelled data. cleanroom-id-map.json
                      resolves the anonymised ids in the clean-room training
                      data back to these packet ids.

                      predictions-external.jsonl scores align-v1's own
                      alignment-based readout against the openhands labels —
                      `python -m bench external ... score` — a separate
                      question from the shipped rule's, kept as its own sheet
                      of record.

                      predictions-rootse.jsonl scores align-v1's profiles
                      against RootSE's external labels. That data is a ~200MB
                      third-party checkout and is not vendored:
                        git clone https://github.com/LogAnalysisTech/TrajAudit \
                          corpus/external/TrajAudit
                      then `python -m bench rootse-eval` (or set ROOTSE_ROOT).
  external/           third-party checkouts that are not vendored, chiefly
                      TrajAudit for RootSE (~600MB). Clone it yourself; see the
                      alignment/ note above.
  counterfactual/     output from the intervention experiments.
  archive-prefix11/   superseded runs from earlier agent versions. Never merge
                      these with store/ — its own README says why.
