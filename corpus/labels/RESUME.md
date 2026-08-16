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

## Holdout-2: labelled, checked

All 140 exported and labelled. Verified independently before scoring:

* the exported `key.jsonl` matches a fresh `draw_test` re-run byte for byte —
  the draw was not perturbed after export;
* zero instance overlap with `calibration/`, any first-pass set, or RootSE;
* stratification matches the plan exactly (405b×25, 70b×50, 8b×25, gpt-4o×40);
* every label is within `[1, step_count]` for its packet;
* label position is not clustered at an extreme: 22% at step 1 ("never got
  started"), 4% at the last step, median at 49% of the trace;
* the two `E2`s and three `E3`s all hold up on inspection — both `E2`s are
  genuine tooling failures with no sound edit preceding them, consistent with
  the C023 standard; the `E3`s are runs whose own trace shows a verified
  working fix despite carrying a failing-run label.

**Found and fixed:** four packets (T090, T094, T099, T103) had two label lines
each, not marked as errata — an accidental overlap at a session-resume
boundary rather than a deliberate correction. All four agree on the step
number; T099 disagrees on confidence between the two passes (`true` then
`false`). Resolved by the protocol's existing append-only, last-line-wins
convention — no data was lost, both lines remain in `labels.jsonl` — and
recorded here rather than silently relied on. The T099 confidence flip is a
second, independent data point (after calibration's confident-subset finding)
that `confident` should not be trusted as a filter.

Final tally after resolving the duplicates: 135 integer labels, 2 `E2`, 3 `E3`.

## Scored. `bench/score_holdout2.py`, run once, verified deterministic on rerun.

135 of 140 items carry an integer label; 5 (`E2`×2, `E3`×3) have no location
to score against.

| | exact (primary) | ±2 | ±5 |
|---|---|---|---|
| `earliest_bound` | **37.8% (51/135)** | 56.3% (76/135) | 64.4% (87/135) |
| `first_commitment` | 37.0% (50/135) | 52.6% (71/135) | 60.0% (81/135) |

At ±2 every item in this set is missable (shortest trace is 7 steps, and
2·2+1=5); at ±5, 121 of 135 are missable — 14 traces are too short to be
wrong on at that tolerance, 10.4%, against 18–39% in the old OpenHands set.
The sampling filters added after the calibration pass (`has_model_prose`,
`learned_something`) appear to have done what they were for.

**This lands close to the published figures** — ±2 at 56.3% against the
in-sample pooled 54% and the first held-out check's 57%, on a set that shares
no instance with either. That is the headline result: a same-order-of-magnitude
replication on fresh data, fresh labels, and a materially different population
(no RootSE at all here; nebius's 405b and 8b strata were never scored before).

**Read every figure above against the calibration band.** Relabelling alone
moved `earliest_bound`'s own accuracy by 18 points at exact match and 12 at
±2 on identical trajectories. 37.8% and 56.3% are not exact — they are the
center of a range roughly that wide.

By reliability class (`earliest_bound`, ±2): `commit-long-single` 100% (n=7,
noisy), `commit-short` 86% (n=49), `silent-short` 60% (n=5, noisy),
`commit-long-many` 36% (n=58), `silent-long` 19% (n=16). Same ordering
`divergence.md` reports (commitment and short beat long and silent), on data
none of that ordering was fitted to.

By stratum (`earliest_bound`, exact / ±2): nebius 405b 30%/52% (n=23), 70b
30%/48% (n=50), 8b 36%/60% (n=25), openhands gpt-4o 54%/68% (n=37). No clean
story by model size — 405b and 8b score within a few points of each other —
but n per stratum is small enough that this should be read as "no evidence
of a strong effect" rather than "no effect."

**Confidence is inverted, and the reason is the most useful thing here.**
`confident=True` scores *worse* than `confident=False`: exact 28% (17/61)
against 46% (34/74); ±2 48% against 64%. Chased down, it is not a fact about
labelling at all.

`confident=True` labels sit at a median of **19%** through their trace;
`confident=False` at **71%**. The flag was never recording "this label is
reliable" — it was recording "this run was obviously doomed early," which is
a different property that happens to feel like certainty while labelling. It
carries the class mix to match: 14 of 61 confident items are `silent-long`
against 2 of 74 unconfident ones.

And where the label sits is what actually drives accuracy, confidence aside:

| label position | n | exact | ±2 | direction of error |
|---|---|---|---|---|
| early (<0.33) | 57 | 26% | 42% | predicts **later** than truth 32/57, median +2 |
| mid (0.33–0.66) | 26 | 38% | 69% | predicts earlier 15/26, median −1 |
| late (>0.66) | 52 | 50% | 65% | predicts earlier 25/52, median 0 |

That is an upper-bound rule behaving exactly like one. All three of
`earliest_bound`'s components assert "no later than X"; when the truth is
step 1 of a forty-step flail, every one of them lands too late and the
minimum of three too-late bounds is still too late. The residual error is
concentrated on runs that were doomed before they generated anything for a
bound to attach to — which is a structural limit of bounding from above, not
a threshold that wants tuning.

**Do not use `confident` as a quality filter anywhere downstream.** The
recorded values stay — they are what produced the finding above — but the
field measured trace shape, not label quality, and a future pass should not
collect it under this name or for this purpose.

### The labels are anchored to writes, and that inflates the held-out number

How often the point of no return precedes the run's first write, by set:

| set | n | never commits | commits, truth before it | truth at or after |
|---|---|---|---|---|
| RootSE (external labels) | 102 | 4% | **44%** | 52% |
| nebius, first pass (ours) | 70 | 27% | 3% | 70% |
| nebius, holdout-2 (ours) | 98 | 18% | 12% | 69% |
| OpenHands, holdout-2 (ours) | 37 | 8% | 8% | 84% |

A fourteen-fold spread between RootSE and our own first-pass nebius labels is
not plausibly a fact about agent runs. It is a fact about labelling, and the
mechanism is visible in where each label lands:

| set | label sits on a step that writes | commonest action at the labelled step |
|---|---|---|
| RootSE (external) | **42%** | `str_replace` 40, **no action at all 18**, `create` 8 |
| nebius, first pass (ours) | 59% | `edit` 44 |
| nebius, holdout-2 (ours) | 68% | `edit` 70, `create` 21 |
| OpenHands, holdout-2 (ours) | **78%** | `str_replace` 25, `insert` 7 |

The TrajAudit annotators put 18 of 102 labels on steps with **no action at
all** — a turn where the agent only reasoned. Ours almost never do. Our
labels land on edits because the standard recorded above told the labeller to
find the last sound write to pre-existing source and judge it, which is a
sound way to label consistently and also the same place `earliest_bound`
looks.

The controlled comparison is the two nebius passes: same pool, comparable
population, 59% against 68%, with the second pass being the one whose written
standard emphasised writes. Population cannot explain that gap; convention
can.

**Consequence for the held-out result.** 56.3% at ±2 is measured against
labels that agree with the rule's own anchor far more often than an
independent annotator's do. RootSE, the only externally labelled set this
project has, is also the set `earliest_bound` scores worst on. The honest
reading of the held-out number is *"agreement with a labeller who was
instructed to look where the rule looks"*, not *"accuracy at locating the
point of no return."* `PROTOCOL.md` flagged this risk in the abstract; this
is the measurement of it.

It also means the write-anchored ceiling quoted earlier (~60% exact) is not a
property of agent runs. Under RootSE's labelling convention the same ceiling
is near 56%; under ours it is near 90%. The ceiling moves with the
convention, which is the clearest possible sign that the target is partly
defined by the instrument.

A future pass that wants an uncontaminated number has to drop the
write-anchoring instruction from the labelling standard and accept the lower
inter-labeller agreement that will follow — or label a set blind to the
rule's existence entirely.
