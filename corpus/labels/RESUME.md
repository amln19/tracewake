# Where the second labelling pass stands

Operational state for a session picking this up cold. `PROTOCOL.md` is the
registered protocol and does not change; this file is the running state.

## Order, which matters

1. ~~Finish `calibration/` — 60 items.~~ Done: all 60 labelled
   (`corpus/labels/calibration/labels.jsonl`), 48 with an integer, 10 `E2`,
   2 `E3`.
2. ~~Score agreement against the first-pass labels.~~ Done, see "Calibration
   result" below.
3. ~~`holdout-2/` — 140 items, exported via `draw_test`/`export`~~ Done: all
   140 labelled (`corpus/labels/holdout-2/labels.jsonl`), packets in
   `corpus/labels/holdout-2/packets`, key in `corpus/labels/holdout-2/key.jsonl`,
   stratified: nebius llama-405b×25, llama-70b×50, llama-8b×25, openhands
   gpt-4o×40; instance-disjoint from RootSE and from everything in
   `calibration/`. The C023 lesson was applied throughout: several items got a
   numeric label at a sound edit that preceded an environment or
   verification obstacle (e.g. broken test credentials, corrupted download
   data), rather than an E2 exclusion.
4. Score once, with the rule frozen. Not started — do not begin without
   confirming with the user first, same as step 2 waited on step 1.

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

## Calibration result

Joined via `calibration/key.jsonl`'s `origin_packet` against
`external/labels.jsonl`, `nebius/labels.jsonl`, and `nebius-holdout/labels.jsonl`
(all three held the first-pass label for some fraction of the 60 origin
packets — the calibration draw pulled from all three, not just `external` and
`nebius` as the earlier text here assumed). All 60 origin packets carry an
integer first-pass label; none were excluded on the first pass.

Of the 60 second-pass labels, 48 were integers, 10 `E2`, 2 `E3`. A follow-up
append (below) supersedes one `E2` with an integer, bringing the comparable
set to 49; the numbers here use that corrected set. Agreement is computed over
the 49 with a second-pass integer (an `E2`/`E3` item has no number to compare)
and, separately, over all 60 (treating any second-pass exclusion as a miss,
since first-pass never excluded).

| window | n comparable | exact/±2/±5 match | mean chance rate | over all 60 |
|---|---|---|---|---|
| exact | 49 | 40.8% (20/49) | 4.1% | 33.3% (20/60) |
| ±2    | 49 | 49.0% (24/49) | 19.7% | 40.0% (24/60) |
| ±5    | 49 | 69.4% (34/49) | 38.0% | 56.7% (34/60) |

All three windows clear their chance floor by a wide margin, which says the
rubric in `PROTOCOL.md` is reproducing what the first pass meant by "point of
no return" rather than guessing near the middle of each trace. But two further
cuts of the same 49 pairs matter more than the headline table, and both argue
for reporting the held-out result cautiously rather than for redoing this pass:

**The rule's own measured accuracy is not stable under which label set scores
it.** `earliest_bound` run on these same 49 trajectories:

| | exact | ±2 |
|---|---|---|
| vs first-pass label | 31% (15/49) | 49% (24/49) |
| vs second-pass label | 45% (22/49) | 61% (30/49) |

An 18-point swing at exact match and a 12-point swing at ±2, from relabelling
alone, on identical trajectories. That is on the same order as the gaps
`divergence.md` treats as meaningful between rules. Any accuracy number from
holdout-2 should be read as sitting inside a band roughly this wide, not as a
point estimate.

**Confidence does not predict agreement.** Restricting to the 15 pairs marked
`confident: true`: exact 47% (7/15), ±2 47% (7/15), ±5 73% (11/15) — no better
than the full set at exact and ±2, worse at exact than the unrestricted mean
would suggest. With n=15 this is not a strong claim, but it means `confident`
should not be used as a filter to produce a cleaner headline number from
holdout-2; report the full set.

### Erratum, applied: C023 was excluded too readily

Every one of the 12 second-pass exclusions maps to an origin packet that
*did* get an integer on the first pass. For 11 of them this is not a
disagreement: this pass's step counts match the first pass's `bad_steps`
exactly on every one, so there is no render gap — the same trajectory was
available to both passes, and the first pass assigned an integer to what is,
on this rubric, an unlabellable run (an empty directory listing on repeat, or
an infrastructure failure with no prior committing edit). That is evidence
the first-pass label set carries known noise on short and degenerate
trajectories, consistent with the `empty_generation` contamination found
earlier in this project's evaluation — not evidence this pass's rubric is
wrong.

C023 is the one exception. It was excluded `E2` for repeated 503s from step 11
onward, but step 7 landed a real, judgeable edit (`.clone()` on `ssim_value`)
*before* the infrastructure failure started. `PROTOCOL.md`'s own standard —
label where recovery stopped being possible — argues for labelling at that
last sound edit and treating the subsequent 503s as where the trace ends, not
as grounds to exclude the item. C027 in the same batch was checked against
this identical question and holds up as `E2`: no edit landed and applied
cleanly before its own 504/503 run began, so there is no commitment point to
fall back to.

Applied as an append, not a silent edit — `calibration/labels.jsonl` retains
the original `C023: E2` line and a second `C023` line marked
`"supersedes": "C023 E2"` with label `7`. The scoring above uses the
superseding line.

**For holdout-2:** do not exclude an item on an infrastructure or environment
failure without checking whether a real, applied edit to pre-existing source
landed before the failure began. If one did, label it there; the failure is
where the trace ends, not grounds to exclude.

## Next: holdout-2

Export the held-out set only after the above is settled:

    from bench.relabel import draw_test, export
    export(draw_test(rootse_instance_ids), "holdout-2")
