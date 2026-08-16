"""A ~70/30 train/test partition per dataset, and the score on it.

The three sources reached their splits by different routes and the resulting
numbers do not mean the same thing. This module keeps that visible rather than
averaging it away.

  openhands   80 train / 37 test, 2 test items demoted to train for the ratio.
  nebius      70 train / 98 test, 48 demoted.
  rootse      102 items, none of which were ever held out. The split below was
              created after the rule was selected against all 102, so its "test"
              side is in-sample and its score is optimistically biased by an
              unknown amount. It is reported because a per-dataset split was
              asked for, not because it measures generalization.

Demotion is safe in one direction only. Moving a held-out item into training
discards evidence; it cannot contaminate what remains. The reverse would be
fabrication, which is why nothing here promotes a training item to test.

Two honest caveats on the demoted sets. Choosing which items move is done by
seed, never by whether the rule got them right, or the remainder would be
selected for. And holdout-2 was already scored once as a whole, so any figure
computed on a subset of it is that set's second use and no longer a clean
single-shot measurement.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .repos import CORPUS_ROOT

# Fixed before the partition was drawn.
DEMOTE_SEED = 20260816
ROOTSE_SPLIT_SEED = 20260817

DEMOTE = {"openhands": 2, "nebius": 48}
ROOTSE_TEST = 31

SPLIT_PATH = CORPUS_ROOT / "alignment" / "split-70-30.json"


def _holdout2() -> tuple[dict[str, dict], dict[str, dict]]:
    root = CORPUS_ROOT / "labels" / "holdout-2"
    key = {json.loads(l)["packet_id"]: json.loads(l) for l in (root / "key.jsonl").read_text().splitlines() if l.strip()}
    final: dict[str, dict] = {}
    for line in (root / "labels.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            final[row["packet_id"]] = row
    return key, final


def build() -> dict:
    """The partition. Deterministic; safe to re-run."""
    key, final = _holdout2()
    scored = {p: r for p, r in final.items() if r.get("label") is not None}
    rng = random.Random(DEMOTE_SEED)
    demoted: dict[str, list[str]] = {}
    for source, count in sorted(DEMOTE.items()):
        pool = sorted(p for p in scored if key[p]["source"] == source)
        demoted[source] = sorted(rng.sample(pool, count))

    from .rootse import load_failures

    # Split by instance, not by failure: 102 failures span 98 instances, and the
    # same bug on both sides would leak. Instances are added until the failure
    # count reaches the target, so the target is on failures and the unit is the
    # instance.
    failures = [f for f in load_failures() if f.bad]
    per_instance: dict[str, int] = {}
    for f in failures:
        per_instance[f.instance_id] = per_instance.get(f.instance_id, 0) + 1
    order = sorted(per_instance)
    random.Random(ROOTSE_SPLIT_SEED).shuffle(order)
    rootse_test, taken = [], 0
    for instance in order:
        if taken >= ROOTSE_TEST:
            break
        rootse_test.append(instance)
        taken += per_instance[instance]
    rootse_test = sorted(rootse_test)

    return {
        "created": "2026-08-16",
        "demote_seed": DEMOTE_SEED,
        "rootse_split_seed": ROOTSE_SPLIT_SEED,
        "demoted_to_train": demoted,
        "rootse_test_instances": rootse_test,
        "provenance": {
            "openhands": "held out in holdout-2; 2 items demoted to train for the ratio",
            "nebius": "held out in holdout-2; 48 items demoted to train for the ratio",
            "rootse": (
                "NOT held out. All 102 were used to select the rule. This split was "
                "created 2026-08-16, afterwards. Scores on its test side are in-sample."
            ),
        },
    }


def report() -> str:
    from bench.baselines import earliest_bound

    from .relabel import _bulk_rows, draw_test, load_steps
    from .rootse import load_failures

    split = build()
    key, final = _holdout2()
    demoted = {p for ps in split["demoted_to_train"].values() for p in ps}

    rootse_ids = sorted({f.instance_id for f in load_failures() if f.bad})
    draws = {d.packet_id: d for d in draw_test(rootse_ids)}
    steps: dict[str, list] = {}
    for source in ("nebius", "openhands"):
        items = [d for d in draws.values() if d.source == source]
        rows = _bulk_rows(source, items)
        for d in items:
            steps[d.packet_id] = load_steps(d, rows)

    lines = [
        "~70/30 per-dataset split. Test sides are not equivalent evidence.",
        "",
        f"{'dataset':<12}{'train':<8}{'test':<7}{'test %':<9}{'exact':<14}{'+/-2':<14}{'what the test side is'}",
    ]
    for source, train_n in (("openhands", 80), ("nebius", 70)):
        held = sorted(
            p for p, r in final.items()
            if r.get("label") is not None and key[p]["source"] == source and p not in demoted
        )
        ex = sum(1 for p in held if earliest_bound(steps[p]) == final[p]["label"])
        w2 = sum(1 for p in held if abs(earliest_bound(steps[p]) - final[p]["label"]) <= 2)
        n = len(held)
        total = train_n + DEMOTE[source] + n
        lines.append(
            f"{source:<12}{train_n + DEMOTE[source]:<8}{n:<7}{n / total:<9.1%}"
            f"{f'{ex}/{n} {ex/n:.0%}':<14}{f'{w2}/{n} {w2/n:.0%}':<14}held out, scored twice"
        )

    test_ids = set(split["rootse_test_instances"])
    rs = [(f.label, f.bad) for f in load_failures() if f.bad and f.instance_id in test_ids]
    ex = sum(1 for lab, s in rs if earliest_bound(s) == lab)
    w2 = sum(1 for lab, s in rs if abs(earliest_bound(s) - lab) <= 2)
    n = len(rs)
    lines.append(
        f"{'rootse':<12}{102 - n:<8}{n:<7}{n / 102:<9.1%}"
        f"{f'{ex}/{n} {ex/n:.0%}':<14}{f'{w2}/{n} {w2/n:.0%}':<14}IN-SAMPLE, not held out"
    )
    lines += [
        "",
        "The rootse row is not a held-out measurement. Every one of those 102 runs",
        "informed the choice of rule; the split was drawn afterwards. Its figure is",
        "optimistically biased and belongs beside the in-sample table in",
        "contracts/divergence.md, not beside the holdout-2 result.",
        "",
        "The openhands and nebius rows are a second use of holdout-2, which was",
        "already scored once whole. Demotion was seeded, never chosen by whether the",
        "rule was right, but a subset scored after the whole has been seen is no",
        "longer a clean single-shot number.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    SPLIT_PATH.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report())
