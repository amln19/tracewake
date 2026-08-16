"""Score every localisation rule against every labelled set at once.

Four sets, 178 pairs, and they are not equivalent evidence:

  oh_dev    OpenHands development half (40)  Tracewake's labels; design use allowed
  oh_final  OpenHands held-out half    (40)  Tracewake's labels; spent once
  rootse    RootSE                     (58)  externally labelled by other people
  nebius    nebius/SWE-agent           (40)  Tracewake's labels, written 2026-08-15

Pooling them is useful for ranking rules and misleading for headline accuracy:
the sets differ enormously in trace length and in how much their agents thrash.
Read the per-set columns, not the total.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence

from tracewake.align import LexicalEmbedder, Step, align, divergence_step
from tracewake.diverge import RELIABILITY_BAND, reliability
from bench.baselines import earliest_bound, first_commitment

SETS = ("oh_dev", "oh_final", "rootse", "nebius")
TOLERANCE = 2
Pair = tuple[str, int, list[Step], list[Step]]
Rule = Callable[[Sequence[Step], Sequence[Step]], int]


def load_all() -> dict[str, list[Pair]]:
    """Every labelled pair, keyed by set. Needs `datasets` and `pyarrow`."""
    from bench.divergeeval import load_pairs as load_openhands
    from bench.nebius import LABEL_ROOT
    from bench.partition import load_split
    from bench.rootse import load_pairs as load_rootse

    out: dict[str, list[Pair]] = {}
    for name, final in (("oh_dev", False), ("oh_final", True)):
        labels = load_split(final=final)
        out[name] = [
            (key["packet_id"], labels[key["packet_id"]], good, bad)
            for key, good, bad in load_openhands(sorted(labels))
        ]
    out["rootse"] = [(p.instance_id, p.label, p.good, p.bad) for p in load_rootse()]
    out["nebius"] = _nebius(LABEL_ROOT)
    return out


def _nebius(root) -> list[Pair]:
    import json

    from bench.nebius import _load_rows, _snapshot, to_steps

    key = {
        r["packet_id"]: r
        for r in map(json.loads, (root / "key.jsonl").read_text().splitlines())
    }
    # The two nebius draws live in one directory now but are not one set: this
    # column is the 40 the rule was selected against, and the other 30 were
    # scored once afterwards. Pooling them here would silently restate the
    # figure this table reports.
    first = {r["packet_id"] for r in key.values() if r.get("batch", "nebius-1") == "nebius-1"}
    labels = {
        r["packet_id"]: r["label"]
        for r in map(json.loads, (root / "labels.jsonl").read_text().splitlines())
        if r["label"] is not None and r["packet_id"] in first
    }
    rows = _load_rows(key, sorted(labels), _snapshot())
    return [
        (pid, labels[pid], to_steps(rows[key[pid]["good_row"]]),
         to_steps(rows[key[pid]["bad_row"]]))
        for pid in sorted(labels)
    ]


def rules(embed=None) -> dict[str, Rule]:
    embed = embed or LexicalEmbedder()

    def lexical(good, bad):
        _, alignment, _ = align(good, bad, embed=embed)
        found = divergence_step(alignment, good, bad)
        return found if found is not None else len(bad)

    def commitment(good, bad):
        found = first_commitment(bad)
        return found if found is not None else len(bad)

    return {
        "earliest_bound": lambda good, bad: earliest_bound(bad),
        "first_commitment": commitment,
        "align-v1": lexical,
        "constant-10": lambda good, bad: min(10, len(bad)),
    }


def report(data: dict[str, list[Pair]] | None = None) -> str:
    data = data if data is not None else load_all()
    every = rules()
    lines = [f"within +/-{TOLERANCE}", "", f"{'rule':<20}" + "".join(f"{s:>13}" for s in SETS) + f"{'pooled':>13}"]
    for name, rule in every.items():
        row, hits, total = "", 0, 0
        for s in SETS:
            h = sum(abs(rule(g, b) - t) <= TOLERANCE for _p, t, g, b in data[s])
            n = len(data[s])
            hits, total = hits + h, total + n
            row += f"{h:>4}/{n:<3}{h / n:>5.0%}".rjust(13)
        lines.append(f"{name:<20}{row}{f'{hits}/{total} {hits / total:.0%}':>13}")

    lines += ["", "reliability of earliest_bound", ""]
    lines.append(f"{'class':<21}" + "".join(f"{s:>12}" for s in SETS) + f"{'pooled':>14}")
    totals: dict[str, tuple[int, int]] = {}
    for klass in RELIABILITY_BAND:
        row, hits, total = "", 0, 0
        for s in SETS:
            sub = [(t, b) for _p, t, _g, b in data[s] if reliability(b) == klass]
            h = sum(abs(earliest_bound(b) - t) <= TOLERANCE for t, b in sub)
            hits, total = hits + h, total + len(sub)
            row += (f"{h:>3}/{len(sub):<3}" if sub else "   -  ").rjust(12)
        totals[klass] = (hits, total)
        tail = f"{hits:>3}/{total:<4}{hits / total:>5.0%}" if total else ""
        lines.append(f"{klass:<21}{row}{tail:>14}")

    lines += ["", "risk-coverage (abstain from the bottom classes)", ""]
    keep: list[str] = []
    grand = sum(n for _h, n in totals.values())
    for klass in RELIABILITY_BAND:
        keep.append(klass)
        hits = sum(totals[k][0] for k in keep)
        total = sum(totals[k][1] for k in keep)
        lines.append(f"  coverage {total / grand:>4.0%}   accuracy {hits / total:>4.0%}   {'+'.join(keep)}")
    return "\n".join(lines)


def mean_absolute_error(data: dict[str, list[Pair]], rule: Rule, which: str) -> float:
    return statistics.mean(abs(rule(g, b) - t) for _p, t, g, b in data[which])


if __name__ == "__main__":
    print(report())
