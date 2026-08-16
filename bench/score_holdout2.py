"""Score once, rule frozen, per corpus/labels/PROTOCOL.md. Run exactly once."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from bench.relabel import draw_test, _bulk_rows, load_steps
from tracewake.diverge import earliest_bound, first_commitment, reliability

ROOT = Path("corpus/labels")


def chance(label: int, n: int, k: int) -> float:
    return (min(n, label + k) - max(1, label - k) + 1) / n


def main() -> None:
    rootse_ids = json.loads(
        Path(
            "/private/tmp/claude-501/-Users-amlan-Documents-MyProjects-tracewake/"
            "4268901a-bb3f-49fe-99eb-775bd6df87b1/scratchpad/rootse_instances.json"
        ).read_text()
    )
    draws = {d.packet_id: d for d in draw_test(rootse_ids)}
    key = {json.loads(l)["packet_id"]: json.loads(l) for l in (ROOT / "holdout-2/key.jsonl").read_text().splitlines() if l.strip()}
    final: dict[str, dict] = {}
    for line in (ROOT / "holdout-2/labels.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            # Append-only labels: a superseding line wins over the one it
            # supersedes. See corpus/labels/PROTOCOL.md.
            final[row["packet_id"]] = row

    steps: dict[str, list] = {}
    for source in ("nebius", "openhands"):
        items = [d for d in draws.values() if d.source == source]
        rows = _bulk_rows(source, items)
        for d in items:
            steps[d.packet_id] = load_steps(d, rows)

    items = []
    excluded = 0
    for pid, row in final.items():
        if row.get("label") is None:
            excluded += 1
            continue
        items.append((pid, row["label"], row.get("confident"), key[pid]["source"], key[pid]["model"], steps[pid]))

    n = len(items)
    print(f"scored {n} items ({excluded} excluded: E2/E3, no location to predict)\n")

    print("=== earliest_bound ===")
    eb_preds = {pid: earliest_bound(s) for pid, _, _, _, _, s in items}
    exact = sum(1 for pid, label, *_ in items if eb_preds[pid] == label)
    print(f"exact match (primary):  {exact}/{n} = {exact/n:.1%}")
    for k in (2, 5):
        hits = sum(1 for pid, label, *_, s in items if abs(eb_preds[pid] - label) <= k)
        missable = sum(1 for pid, label, *_, s in items if chance(label, len(s), k) < 1.0)
        missable_hits = sum(
            1 for pid, label, *_, s in items
            if chance(label, len(s), k) < 1.0 and abs(eb_preds[pid] - label) <= k
        )
        mean_chance = statistics.mean(chance(label, len(s), k) for _, label, *_, s in items)
        print(
            f"+/-{k}: {hits}/{n} = {hits/n:.1%}  |  on the missable subset ({missable}/{n}): "
            f"{missable_hits}/{missable} = {missable_hits/missable:.1%}  |  mean chance rate {mean_chance:.1%}"
        )

    print("\n=== first_commitment (comparison rule) ===")
    fc_preds = {}
    for pid, _, _, _, _, s in items:
        found = first_commitment(s)
        fc_preds[pid] = found if found is not None else len(s)
    exact = sum(1 for pid, label, *_ in items if fc_preds[pid] == label)
    print(f"exact match: {exact}/{n} = {exact/n:.1%}")
    for k in (2, 5):
        hits = sum(1 for pid, label, *_ in items if abs(fc_preds[pid] - label) <= k)
        print(f"+/-{k}:      {hits}/{n} = {hits/n:.1%}")

    print("\n=== by reliability class (earliest_bound, +/-2) ===")
    by_class: dict[str, list[bool]] = {}
    for pid, label, _, _, _, s in items:
        _, cls = eb_preds[pid], reliability(s)
        by_class.setdefault(cls, []).append(abs(eb_preds[pid] - label) <= 2)
    for cls in sorted(by_class, key=lambda c: -sum(by_class[c]) / len(by_class[c])):
        hits, total = sum(by_class[cls]), len(by_class[cls])
        print(f"  {cls:<20} {hits}/{total} = {hits/total:.0%}")

    print("\n=== by stratum (earliest_bound, exact / +/-2) ===")
    strata: dict[tuple[str, str], list[tuple[bool, bool]]] = {}
    for pid, label, _, source, model, s in items:
        strata.setdefault((source, model), []).append(
            (eb_preds[pid] == label, abs(eb_preds[pid] - label) <= 2)
        )
    for (source, model), vals in sorted(strata.items()):
        exact_n = sum(v[0] for v in vals)
        w2_n = sum(v[1] for v in vals)
        print(f"  {source:<10} {model:<24} n={len(vals):<4} exact {exact_n}/{len(vals)}  +/-2 {w2_n}/{len(vals)}")

    print("\n=== confidence check (earliest_bound, exact / +/-2) ===")
    for want in (True, False):
        subset = [(pid, label) for pid, label, conf, *_ in items if conf is want]
        if not subset:
            continue
        exact_n = sum(1 for pid, label in subset if eb_preds[pid] == label)
        w2_n = sum(1 for pid, label in subset if abs(eb_preds[pid] - label) <= 2)
        print(f"  confident={want!s:<5} n={len(subset):<4} exact {exact_n}/{len(subset)}  +/-2 {w2_n}/{len(subset)}")


if __name__ == "__main__":
    main()
