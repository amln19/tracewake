"""Score a predictor against the clean-room training set.

    python score.py                 # whole training set, per source
    python score.py --cv            # 5-fold cross-validation
    python score.py --baselines     # trivial predictors
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path

DATA = Path(__file__).parent / "data" / "train.jsonl"
TOLERANCES = (0, 2, 5)


def load() -> list[dict]:
    return [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines() if line.strip()]


def chance(label: int, n: int, k: int) -> float:
    return (min(n, label + k) - max(1, label - k) + 1) / n


def evaluate(predict, rows: list[dict]) -> dict:
    out = {}
    for k in TOLERANCES:
        hits = missable = missable_hits = 0
        floor = []
        for row in rows:
            n = len(row["steps"])
            ok = abs(predict(row["steps"]) - row["label"]) <= k
            hits += ok
            floor.append(chance(row["label"], n, k))
            if floor[-1] < 1.0:
                missable += 1
                missable_hits += ok
        out[k] = {
            "accuracy": hits / len(rows),
            "chance": statistics.mean(floor),
            "missable": missable,
            "missable_accuracy": missable_hits / missable if missable else float("nan"),
        }
    return out


def show(title: str, result: dict) -> None:
    print(f"\n{title}")
    print(f"  {'window':<10}{'accuracy':<14}{'on missable':<20}{'chance'}")
    for k, row in result.items():
        label = "exact" if k == 0 else f"+/-{k}"
        missable = f"{row['missable_accuracy']:.1%} ({row['missable']})" if row["missable"] else "n/a"
        print(f"  {label:<10}{row['accuracy']:<14.1%}{missable:<20}{row['chance']:.1%}")


def cross_validate(predict, rows: list[dict], folds: int = 5) -> None:
    order = list(rows)
    random.Random(0).shuffle(order)
    buckets = [order[i::folds] for i in range(folds)]
    print(f"\n{folds}-fold cross-validation")
    print(f"  {'fold':<8}{'exact':<12}{'+/-2':<12}{'+/-5'}")
    scores = {k: [] for k in TOLERANCES}
    for i, bucket in enumerate(buckets, start=1):
        result = evaluate(predict, bucket)
        for k in TOLERANCES:
            scores[k].append(result[k]["accuracy"])
        print(f"  {i:<8}{result[0]['accuracy']:<12.1%}{result[2]['accuracy']:<12.1%}{result[5]['accuracy']:.1%}")
    print(f"  {'mean':<8}{statistics.mean(scores[0]):<12.1%}{statistics.mean(scores[2]):<12.1%}{statistics.mean(scores[5]):.1%}")
    print(f"  {'stdev':<8}{statistics.stdev(scores[0]):<12.1%}{statistics.stdev(scores[2]):<12.1%}{statistics.stdev(scores[5]):.1%}")


def baselines(rows: list[dict]) -> None:
    for name, predict in (
        ("always step 1", lambda steps: 1),
        ("last step", lambda steps: len(steps)),
        ("middle of trace", lambda steps: max(1, round(0.5 * len(steps)))),
    ):
        show(f"baseline: {name}", evaluate(predict, rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cv", action="store_true", help="5-fold cross-validation")
    parser.add_argument("--baselines", action="store_true", help="score trivial predictors")
    args = parser.parse_args()
    rows = load()
    if args.baselines:
        baselines(rows)
        return
    try:
        from predictor import predict
    except ImportError:
        print("No predictor.py yet. Run `python score.py --baselines` first.")
        return
    if args.cv:
        cross_validate(predict, rows)
        return
    show(f"whole training set (n={len(rows)})", evaluate(predict, rows))
    for source in sorted({row["source"] for row in rows}):
        show(f"source {source}", evaluate(predict, [row for row in rows if row["source"] == source]))


if __name__ == "__main__":
    main()
