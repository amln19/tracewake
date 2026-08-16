"""Score the clean-room predictor on the withheld sets. Run once.

Two questions, kept apart because they are not equally answerable.

The paired comparison against `earliest_bound` on holdout-2 is fair: both rules
are out-of-sample there, both read the same trajectories, and both are scored
against the same labels, right or wrong. McNemar over the discordant items is
the test, not the gap between two percentages.

RootSE is not a fair comparison and is reported anyway. The rebuild never saw
it; the incumbent was selected against it. So a rebuild win there is understated
and a rebuild loss is confounded three ways -- in-sample incumbent, unseen
scaffolds, and labels written by people with no stake in either rule. It is the
only externally labelled set this project has, which is why it is worth the
caveat.
"""

from __future__ import annotations

import collections
import json
import math
import pathlib
import statistics
import sys

from .repos import CORPUS_ROOT

CLEANROOM = pathlib.Path(__file__).resolve().parents[2] / "tracewake-cleanroom"
PARTITION = CORPUS_ROOT / "alignment" / "cleanroom-partition.json"


def _as_dicts(steps) -> list[dict]:
    """Trajectory in the shape the clean room was handed."""
    return [
        {
            "action": s.name,
            "target": s.target,
            "args": s.args,
            "reasoning": s.reasoning,
            "observation": s.observation,
            "writes": sorted(s.writes),
            "batch_actions": list(s.batch_names),
        }
        for s in steps
    ]


def chance(label: int, n: int, k: int) -> float:
    return (min(n, label + k) - max(1, label - k) + 1) / n


def _load_test() -> dict[str, list[tuple[str, int, list]]]:
    """Every withheld trajectory, grouped by pool."""
    import pyarrow.parquet as pq

    from .external import strip_terminal, to_steps as oh_steps
    from .nebius import _snapshot, to_steps as neb_steps
    from .relabel import _bulk_rows, _openhands_snapshot, draw_test, load_steps
    from .rootse import load_failures

    split = json.loads(PARTITION.read_text())
    wanted = {uid for src in split for uid in split[src]["test"]}
    pools: dict[str, list] = collections.defaultdict(list)

    # older nebius, whatever the partition left in test
    root = CORPUS_ROOT / "labels" / "nebius"
    key = {json.loads(l)["packet_id"]: json.loads(l) for l in (root / "key.jsonl").read_text().splitlines() if l.strip()}
    labs = {
        json.loads(l)["packet_id"]: json.loads(l)["label"]
        for l in (root / "labels.jsonl").read_text().splitlines()
        if l.strip() and json.loads(l).get("label") is not None
    }
    need = collections.defaultdict(set)
    for p in labs:
        if f"neb-old/{p}" in wanted:
            shard, index = key[p]["bad_row"].rsplit(":", 1)
            need[shard].add(int(index))
    cache, snapshot = {}, _snapshot()
    for shard, indexes in need.items():
        column = pq.read_table(snapshot / shard, columns=["trajectory"])["trajectory"]
        for index in sorted(indexes):
            cache[f"{shard}:{index}"] = column[index].as_py()
        del column
    for p, label in labs.items():
        if f"neb-old/{p}" in wanted:
            steps = neb_steps(cache[key[p]["bad_row"]])
            if steps:
                pools["nebius"].append((f"neb-old/{p}", label, steps))

    # older openhands
    root = CORPUS_ROOT / "labels" / "openhands"
    key = {json.loads(l)["packet_id"]: json.loads(l) for l in (root / "key.jsonl").read_text().splitlines() if l.strip()}
    labs = {
        json.loads(l)["packet_id"]: json.loads(l)["label"]
        for l in (root / "labels.jsonl").read_text().splitlines()
        if l.strip() and json.loads(l).get("label") is not None
    }
    # (instance_id, run_id) is NOT unique in this dataset: 197 pairs name two
    # rollouts each. The key records how many steps the labelled trajectory had,
    # so that breaks the tie. A packet whose length still matches two rows is
    # genuinely ambiguous and is dropped rather than guessed at.
    want_runs = {(key[p]["instance_id"], key[p]["bad_run_id"]): p for p in labs if f"oh-old/{p}" in wanted}
    candidates: dict[str, list] = collections.defaultdict(list)
    for shard in sorted(_openhands_snapshot().glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "run_id", "messages"])
        for instance, run, messages in zip(
            table["instance_id"].to_pylist(), table["run_id"].to_pylist(), table["messages"].to_pylist(), strict=True
        ):
            p = want_runs.get((instance, run))
            if p:
                steps = strip_terminal(oh_steps(messages, shell_verbs=True))
                if steps:
                    candidates[p].append(steps)
    for p, options in candidates.items():
        if len(options) > 1:
            expected = key[p].get("bad_steps")
            options = [s for s in options if len(s) == expected] or []
        if len(options) != 1:
            continue
        pools["openhands"].append((f"oh-old/{p}", labs[p], options[0]))

    # holdout-2, kept whole by the partition
    rootse_ids = sorted({f.instance_id for f in load_failures() if f.bad})
    draws = {d.packet_id: d for d in draw_test(rootse_ids)}
    h2 = {}
    for line in (CORPUS_ROOT / "labels" / "holdout-2" / "labels.jsonl").read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            h2[row["packet_id"]] = row
    for source in ("nebius", "openhands"):
        chosen = [d for d in draws.values() if d.source == source]
        rows = _bulk_rows(source, chosen)
        for d in chosen:
            row = h2.get(d.packet_id)
            if row and row.get("label") is not None and f"h2/{d.packet_id}" in wanted:
                pools[source].append((f"h2/{d.packet_id}", row["label"], load_steps(d, rows)))
                pools["holdout-2"].append((f"h2/{d.packet_id}", row["label"], load_steps(d, rows)))

    for failure in load_failures():
        if failure.bad:
            pools["rootse"].append((f"rootse/{failure.instance_id}", failure.label, failure.bad))
    return pools


def _row(name: str, items: list, predict) -> str:
    """`predict` takes the raw Step objects; wrappers handle any conversion."""
    n = len(items)
    cells = ""
    for k in (0, 2, 5):
        hits = sum(1 for _, label, steps in items if abs(predict(steps) - label) <= k)
        floor = statistics.mean(chance(label, len(steps), k) for _, label, steps in items)
        cells += f"{hits}/{n} {hits/n:>5.1%} (ch {floor:.0%})".ljust(24)
    return f"  {name:<14}{cells}"


def mcnemar(items: list, a, b, k: int) -> tuple[int, int, float]:
    """Discordant counts and a two-sided p, for two rules on the same items.

    Comparing two marginal percentages throws away the pairing. These rules read
    the same trajectory and are scored against the same label, so what matters is
    the items where exactly one of them is right.
    """
    only_a = only_b = 0
    for _, label, steps in items:
        hit_a = abs(a(steps) - label) <= k
        hit_b = abs(b(steps) - label) <= k
        only_a += hit_a and not hit_b
        only_b += hit_b and not hit_a
    total = only_a + only_b
    if total == 0:
        return only_a, only_b, 1.0
    # Normal approximation with continuity correction; exact enough at these n.
    z = (abs(only_a - only_b) - 1) / math.sqrt(total)
    p = math.erfc(max(z, 0.0) / math.sqrt(2))
    return only_a, only_b, p


def main() -> None:
    sys.path.insert(0, str(CLEANROOM))
    from predictor import predict as rebuilt

    from tracewake.diverge import earliest_bound

    def rebuilt_on(steps):
        return rebuilt(_as_dicts(steps))

    pools = _load_test()

    print("REBUILT PREDICTOR, on data it has never seen\n")
    print(f"  {'pool':<14}{'exact':<24}{'+/-2':<24}{'+/-5'}")
    for name in ("nebius", "openhands", "rootse"):
        print(_row(name, pools[name], rebuilt_on))
    every = pools["nebius"] + pools["openhands"] + pools["rootse"]
    print(_row("ALL", every, rebuilt_on))

    print("\nPAIRED COMPARISON on holdout-2, where both rules are out-of-sample\n")
    items = pools["holdout-2"]
    print(f"  {'rule':<14}{'exact':<24}{'+/-2':<24}{'+/-5'}")
    print(_row("rebuilt", items, rebuilt_on))
    print(_row("earliest_bound", items, earliest_bound))
    print(f"\n  {'window':<10}{'rebuilt only':<16}{'incumbent only':<18}{'McNemar p':<12}verdict")
    for k in (0, 2, 5):
        only_new, only_old, p = mcnemar(items, rebuilt_on, earliest_bound, k)
        label = "exact" if k == 0 else f"+/-{k}"
        verdict = "separates" if p < 0.05 else "not separated"
        print(f"  {label:<10}{only_new:<16}{only_old:<18}{p:<12.3f}{verdict}")

    print("\n  RootSE below is not a fair comparison -- the incumbent was selected")
    print("  against it, the rebuild has never seen it or its scaffolds.")
    print(f"  {'rule':<14}{'exact':<24}{'+/-2':<24}{'+/-5'}")
    print(_row("rebuilt", pools["rootse"], rebuilt_on))
    print(_row("earliest_bound", pools["rootse"], earliest_bound))


if __name__ == "__main__":
    main()
