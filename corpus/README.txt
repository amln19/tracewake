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
  labels/             blinded divergence-labeling packets. Every set has the
                      same shape: packets/ to read, key.jsonl mapping packet ids
                      back to runs (do not open during a labeling pass), and
                      labels.jsonl holding 1-based FAILURE step indices, append
                      only. Each set past the first carries its own README.txt
                      saying how it was drawn and what it scored.

                      FIRST PASS — all spent. The rule was selected while
                      looking at all of these, so none can give an unbiased
                      estimate of anything built afterwards.
                        packets/          the original alignment set.
                        openhands/        OpenHands transfer sheet, 80 packets.
                                          `bench external score`. Roughly a fifth
                                          of these sit on rollouts where the
                                          model emitted nothing at all; see
                                          contracts/divergence.md.
                        nebius/           SWE-agent, 70 packets in two batches
                                          that share a directory and are not one
                                          set: `nebius-1` (40) is what the rule
                                          was selected against, `nebius-2` (30)
                                          was drawn later and scored once. Every
                                          consumer filters on the `batch` field;
                                          pooling them would restate a held-out
                                          number as an in-sample one.

                      SECOND PASS — drawn by bench/relabel.py after the rule was
                      frozen, protocol registered first.
                        PROTOCOL.md       what was committed to before the first
                                          label existed. Do not edit it; its
                                          value is that git shows it predates
                                          every label it governs.
                        calibration/      60 first-pass items relabelled blind,
                                          to measure whether the two passes mean
                                          the same thing. They agree well above
                                          chance, but the rule's own score moves
                                          18 points at exact match depending on
                                          which pass scores it.
                        holdout-2/        140 fresh trajectories, instance
                                          disjoint from everything above and from
                                          RootSE. Scored once by
                                          `uv run --group bench python -m bench.score_holdout2`.

                      The labels in every Tracewake-written set sit on a step
                      that writes far more often than RootSE's external labels
                      do, and the rule reads writes. Absolute percentages from
                      these sets are partly a measure of that agreement.

                      RENAMED. labels/external/ is now labels/openhands/, and
                      labels/nebius-holdout/ became labels/nebius-2/ and then
                      merged into labels/nebius/ as batch `nebius-2`. The first
                      name collided with RootSE, which is the set that actually
                      carries external labels; the second stopped being true the
                      moment that set was scored. Older commits, and the frozen
                      alignment/partition.json, still name the old paths. Packet
                      ids keep their original prefixes -- E for the OpenHands
                      set, H for nebius-2 -- because they are referenced from
                      labels.jsonl, the calibration key and contracts/, and
                      renaming them would buy nothing.
  alignment/          prediction sheets and external_scout.json (source inventory).
                      partition.json splits the 80 external packets into a
                      development half and a held-out half; dev-fitted.json holds
                      the constants fitted on the development half. Both are
                      written once and refuse to be rewritten — re-splitting or
                      refitting after seeing held-out results would turn a
                      prediction into a fit. predictions-dev.jsonl and
                      predictions-final.jsonl are the two halves, scored by
                      `python -m bench diverge-eval [--final]`.

                      predictions-rootse.jsonl scores the profiles against
                      RootSE's external labels. That data is a ~200MB
                      third-party checkout and is not vendored:
                        git clone https://github.com/LogAnalysisTech/TrajAudit \
                          corpus/external/TrajAudit
                      then `python -m bench rootse-eval` (or set ROOTSE_ROOT).

                      Fixed, 2026-08-16: E10, E36 and E76 used to load
                      byte-identical on both sides, and were recorded here as
                      noise. They were not. A run id names the sampling
                      configuration rather than the rollout, so one
                      (instance, run) pair can name several rollouts — 197 of
                      them do — and the pair loader kept only the last, which
                      handed the same row to both sides whenever a packet's two
                      sides shared a configuration. Seven packets were affected
                      (E10, E36, E43, E45, E70, E76, E79). The loader now picks
                      with the two facts the key records, which side resolved and
                      how many steps it had, and all seven load distinctly. The
                      labels were always fine.
  external/           third-party checkouts that are not vendored, chiefly
                      TrajAudit for RootSE (~600MB). Clone it yourself; see the
                      alignment/ note above.
  counterfactual/     output from the intervention experiments.
  archive-prefix11/   superseded runs from earlier agent versions. Never merge
                      these with store/ — its own README says why.
