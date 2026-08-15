# Where the second labelling pass stands

Operational state for a session picking this up cold. `PROTOCOL.md` is the
registered protocol and does not change; this file is the running state.

## Order, which matters

1. Finish `calibration/` — 60 items, of which 11 are done.
2. Score agreement against the first-pass labels. Only then open them.
3. Label `holdout-2/` — 140 items, not yet exported.
4. Score once, with the rule frozen.

Calibration comes first so that a rubric which turns out not to reproduce the
earlier labels can be fixed while the expensive half is still unspent. Do not
start step 3 before step 2 is written down.

## Blinding

While labelling calibration, do not open:

* `external/labels.jsonl`, `nebius/labels.jsonl`, `nebius-holdout/labels.jsonl`
* the `origin_packet` field in `calibration/key.jsonl`, which names the
  first-pass packet each item reproduces

One disclosure about the eleven labels already written: an earlier analysis
loaded `external/labels.jsonl` programmatically to compare accuracy on
degenerate rollouts, and printed only aggregates — counts and percentages
across 16 and 64 packets. No individual label was displayed. One aggregate,
"label==1 in 0/16", is a weak fact about the old labels; every packet it covers
was excluded as `E2` rather than given an integer, so it moved nothing.

## Doing the work

    ls corpus/labels/calibration/packets              # 60 packets, C001..C060
    cut -d'"' -f4 corpus/labels/calibration/labels.jsonl   # what is already done

Read the packet. Decide. Append one line to
`corpus/labels/calibration/labels.jsonl`:

    {"packet_id": "C012", "label": 7, "confident": true, "note": "..."}
    {"packet_id": "C013", "label": null, "exclusion": "E2", "confident": true, "note": "..."}

Append only. A label found wrong later becomes a recorded erratum, never a
silent edit. Commit after each batch; nothing depends on session state.

## The standard the first eleven were held to

Read every step. The label is where recovery stopped being possible, not where
the run first erred and not where failure became visible. Two habits did most
of the work:

* **Find the last write to pre-existing source, and ask whether it was sound.**
  Most of these runs commit once and then spend their remaining steps on a
  scratch script that cannot detect the problem. C024 edited moto's backend at
  step 4 and spent five more steps testing boto3's client region, which is not
  what it changed.
* **Distrust a packet that looks unlabellable.** Twice now the trajectory was
  degenerate rather than wrong, and both times the automated filter had missed
  it. When a run looks like it never got started, check the raw row before
  forcing an integer:

      test_result.report.empty_generation   # produced no patch
      assistant messages with empty content # model said nothing
      distinct observations across steps    # 1 means it learned nothing

Worked examples, all in `calibration/labels.jsonl` with their reasoning: C001
(defect enters at the edit that applied, not the one that was rejected), C009 (a
malformed edit corrupts project source), C024 (commitment at the only real
edit), C032 (edits on the strength of a repro that proved nothing), C018 (broken
environment, excluded).

## What must not drift

The draw is seeded and committed; do not redraw. The rubric is frozen; changing
it invalidates the set. Exclusions are recorded, never backfilled with redraws —
replacing an excluded item would select for labellable cases. Confidence is
decided when the label is written and never revised.

## After calibration

Agreement is reported three ways: exact, within ±2, and within ±5, each with
the chance rate beside it per `PROTOCOL.md`, and each computed twice — over all
60, and over the 54 that are not `E2`. The second is the number that says
whether these labels mean what the earlier ones did. The first says how much of
the old set was answerable at all.

Export the held-out set only after that is written down:

    from bench.relabel import draw_test, export
    export(draw_test(rootse_instance_ids), "holdout-2")
