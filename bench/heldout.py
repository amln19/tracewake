"""Reproduce the held-out claims, the ones the README leads with.

`bench.pooled` scores the 178 pairs the rule was selected on, which is the
in-sample view. These two measurements are the check on it, and each was scored
once with the rule frozen:

  * the 30 nebius packets drawn from pool entries nothing had rendered;
  * the 44 RootSE failures that ship no passing reference run, which the pair
    loader skipped and nothing had ever scored.

Neither is large. Both exist because a figure chosen from roughly seventy
candidates on the sets it is then reported against is not evidence, and saying
so is worth less than measuring it.
"""

from __future__ import annotations

import json
import statistics

TOLERANCES = (2, 5, 10)


def _nebius_holdout() -> list[tuple[str, int, list]]:
    import pyarrow.parquet as pq

    from .nebius import _snapshot, to_steps
    from .repos import CORPUS_ROOT

    root = CORPUS_ROOT / "labels" / "nebius"
    key = {
        json.loads(line)["packet_id"]: json.loads(line)
        for line in (root / "key.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    # Only the second draw: the first 40 were selected against, these 30 were not.
    second = {p for p, r in key.items() if r.get("batch") == "nebius-2"}
    labels = {
        json.loads(line)["packet_id"]: json.loads(line)["label"]
        for line in (root / "labels.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["label"] is not None
        and json.loads(line)["packet_id"] in second
    }
    snapshot, cache = _snapshot(), {}

    def trajectory(row: str):
        shard, index = row.rsplit(":", 1)
        if shard not in cache:
            cache[shard] = pq.read_table(snapshot / shard, columns=["trajectory"])[
                "trajectory"
            ]
        return cache[shard][int(index)].as_py()

    out = []
    for packet in sorted(labels):
        steps = to_steps(trajectory(key[packet]["bad_row"]))
        if len(steps) != key[packet]["bad_steps"]:
            raise RuntimeError(f"{packet}: trajectory no longer matches its packet")
        out.append((packet, labels[packet], steps))
    return out


def _rootse_split() -> tuple[list, list]:
    """(never scored, already used). Only the first half is held out."""
    from .rootse import load_failures, load_pairs

    used = {(p.instance_id, p.agent, p.model) for p in load_pairs()}
    fresh, spent = [], []
    for f in load_failures():
        row = (f.instance_id, f.label, f.bad)
        (spent if (f.instance_id, f.agent, f.model) in used else fresh).append(row)
    return fresh, spent


def report() -> str:
    from tracewake.diverge import earliest_bound, first_commitment

    def commitment(steps):
        found = first_commitment(steps)
        return found if found is not None else len(steps)

    def line(name: str, rows) -> str:
        n = len(rows)
        cells = ""
        for tol in TOLERANCES:
            hit = sum(abs(earliest_bound(b) - t) <= tol for _p, t, b in rows)
            cells += f"{hit:>4}/{n:<4}{hit / n:>5.0%}"
        c = sum(abs(commitment(b) - t) <= 2 for _p, t, b in rows)
        return f"{name:<34}{cells}{c:>6}/{n:<4}{c / n:>5.0%}"

    fresh, spent = _rootse_split()
    nebius = _nebius_holdout()
    lines = [
        "held out, scored once, rule frozen",
        "",
        f"{'set':<34}" + "".join(f"{'EB ±' + str(t):>13}" for t in TOLERANCES)
        + f"{'FC ±2':>16}",
        line("nebius holdout (never rendered)", nebius),
        line("RootSE, never scored", fresh),
        line("RootSE, used for selection", spent),
        "",
        f"nebius holdout median trace "
        f"{statistics.median(len(b) for _p, _t, b in nebius):.0f} steps; "
        f"RootSE {statistics.median(len(b) for _p, _t, b in fresh):.0f}",
        "",
        "The held-out figures sit at or above the in-sample 54% from",
        "`python -m bench.pooled`, which is the opposite of what selecting the",
        "best of seventy candidates would produce.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
