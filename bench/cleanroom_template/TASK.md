# Locate where a failing agent run went irrecoverably wrong

## The problem

You are given trajectories of AI coding agents that attempted to fix a real
software issue and **failed**. Each carries one integer label: the step at which
the run became unrecoverable.

Build something that predicts that step from the trajectory alone. No reference
run, no successful attempt to compare against, no model call at inference time
— the input is one failing trajectory and the output is one step index.

## What the label means

The 1-based index of the **earliest step after which no later step could
plausibly have recovered the run**, without undoing work already done or outside
intervention.

It is deliberately not any of these:

* not the first mistake — a wrong turn the run notices and fixes is not it;
* not where failure became evident — evidence arrives later than commitment;
* not the last step by default.

A step with no action can be the answer, if the decisive commitment is made in
reasoning and later steps merely execute it. Where two adjacent steps both
qualify, the earlier was chosen.

## The data

`data/train.jsonl` contains 107 trajectories, one JSON object per line:

```
{"id": "...", "source": "A|B", "label": 7, "steps": [
   {"action": "edit", "target": "src/x.py", "args": {...},
    "reasoning": "...", "observation": "...", "writes": ["src/x.py"],
    "batch_actions": []},
   ...
]}
```

`label` indexes `steps` from 1. `writes` lists paths that step wrote, as parsed
from the action; it is derived, not ground truth, and is empty for scaffolds
where writes cannot be attributed. `observation` is what the environment
returned. `batch_actions` is non-empty only when several tool calls collapsed
into one step.

Two sources, `A` and `B`, are different agent scaffolds with different action
vocabularies and failure characteristics. Both were labelled in-house by the
same people.

The held-out sets include a third, unseen scaffold labelled by people
unconnected to this project. A predictor tuned to the surface details of `A`
and `B` will not generalise. Prefer whatever does.

## How you will be scored

**Exact match is the primary figure.** Window accuracy at ±2 and ±5 is
secondary and must be reported with its chance rate, because a window can be
wider than a short trace. For a label at position `L` in a trace of `n` steps,
a uniform random guess lands within `±k` with probability
`(min(n, L+k) − max(1, L−k) + 1) / n`. An item whose chance rate is 1.0 could
not have been gotten wrong and tells you nothing.

`score.py` implements this, including k-fold cross-validation. Use it.

## Two properties you should know before you start

**The labels are noisy.** Independent relabelling moves a fixed predictor's
score by roughly 12 points at ±2 and 18 at exact match. Improvements smaller
than that are not measurable here. Chasing them will overfit the 107 examples.

**Trace lengths vary enormously** — from single digits to a few hundred steps
— and length correlates with much of the data. Check that a predictor is not
secretly a function of trace length.

## The rules

1. Develop on `data/train.jsonl` only. A held-out test set exists; you cannot
   request it.
2. Cross-validate. Anything selected by its score on all 107 is selected on
   its own evaluation. Run `python score.py --cv`.
3. Pre-register exactly one final predictor in `SUBMISSION.md` before the test
   set is touched: what it computes, why, and what you expect it to score.
4. Prefer simple predictors. A fitted constant must beat a parameter-free
   alternative by more than the noise floor to be worth keeping.
5. Report rejected signals as well as the one you keep.

## What to deliver

* `predictor.py` exposing `predict(steps: list[dict]) -> int`, 1-based.
* `SUBMISSION.md` — what it does, why, cross-validated training scores, your
  expected test score, and rejected approaches.
