Agreement set for the second labelling pass. 60 trajectories that already
carried a first-pass label, re-rendered under the protocol in ../README.txt
and labelled again without the old label visible.

  packets/     one markdown file per trajectory, failing side only.
  key.jsonl    packet_id -> instance / model / dataset row, plus origin_packet,
               which names the first-pass packet each item reproduces. Not to be
               opened while labelling.
  labels.jsonl `label` is the 1-based FAILURE step. Append only; a label found
               wrong later gets a superseding line, never a silent edit.

Drawn by bench.relabel.draw_calibration, seed 20260816, stratified
proportionally across the first-pass sources (openhands 32, nebius 28). All 60
origin packets carry an integer first-pass label; none were excluded then.

WHY IT EXISTS

Agreement between the two passes is the only measurement of whether these
labels mean what the earlier ones meant. Disagreement past a tolerance is a
ceiling: no rule can be shown to score above the rate at which two labellers
agree.

RESULT

48 second-pass integers, 10 E2, 2 E3; one E2 superseded by an erratum (below)
brings the comparable set to 49.

  exact  20/49 = 40.8%   against a 4.1% chance floor
  +/-2   24/49 = 49.0%   against 19.7%
  +/-5   34/49 = 69.4%   against 38.0%

Every window clears its floor by a wide margin, so the rubric reproduces what
the first pass meant by "point of no return". Two cuts of the same 49 pairs
matter more than that table, and both are recorded in contracts/divergence.md
because they bound what any figure in this project can claim:

  * The shipped rule scores 26.5% exact against the first-pass labels and
    44.9% against the second, on identical trajectories -- an 18.4-point swing
    from relabelling alone (13/49 vs 22/49; ±2 swings the same 9 items, 40.8%
    vs 59.2%).
  * The confidence flag does not track label position the way it might seem
    to. confident=True labels (n=15) sit at a median 44% through their trace
    against 33% for confident=False (n=34) -- confident is not simply a proxy
    for "obviously doomed early." Rule accuracy against confident=True is
    80.0% versus 29.4% against confident=False, but n=15 is thin enough that
    this should be read as suggestive, not load-bearing.

ERRATUM, APPLIED

C023 was excluded E2 for repeated 503s from step 11, but step 7 landed a real,
judgeable edit before the infrastructure failure began. Superseded by an
appended line with label 7; the original E2 line remains. C027 was checked
against the same question and holds as E2 -- no edit landed cleanly before its
own failures started.

The other 11 exclusions are not disagreements about evidence. This pass's step
counts match the first pass's bad_steps exactly on all twelve, so both passes
saw the same trajectory; the first pass assigned integers to runs that are
unlabellable on this rubric (empty directory listing on repeat, or an
infrastructure failure with no prior commitment). That is a property of the
first-pass label set, not of this rubric.

BLINDING, AND ONE DISCLOSURE

The first-pass labels files and the origin_packet field were not opened while
labelling. One exception: an earlier analysis loaded openhands/labels.jsonl
programmatically to compare accuracy on degenerate rollouts and printed only
aggregates across 16 and 64 packets. No individual label was displayed. One
aggregate, "label==1 in 0/16", is a weak fact about the old labels; every
packet it covers was excluded as E2 rather than given an integer.
