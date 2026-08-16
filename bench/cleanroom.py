"""Export the training data for an isolated rebuild of the divergence rule.

Every candidate signal this project has tried descends from one idea: writing is
irreversible, so anchor on the first write. Roughly seventy variations on it have
been measured. That record stops the same dead ends being re-run, and it is also
a reason to doubt whether the family was ever the right one — a map and a cage
look identical from inside.

So: hand the labelled training data to an agent that has never seen any of it,
and compare what it builds. The comparison is only worth anything if the
isolation is real, which is why this exports a self-contained file rather than
pointing at the repository. A branch would not do: the history and the object
store come with it, and `git log --all` reaches `contracts/divergence.md` in one
command.

What is withheld: the implementation, the evaluation, every document describing
either, the held-out set, and RootSE. What is given: 150 failing trajectories
from the two in-house sets, their labels, the definition of what the label
means, and the metric. Source names are replaced with A/B and trajectory ids
with opaque ones, so the benchmark cannot be looked up and its published
baselines read off.

RootSE is withheld as a test set rather than spent as development data, which
is the whole reason it is worth anything: it is the only set labelled by people
outside this project, so it is the one measurement immune to the write-anchored
labelling the in-house sets carry. Testing the rebuild on it is out-of-sample;
testing `earliest_bound` on it is not, since RootSE informed that rule's
selection. Any rebuild win there is therefore understated, and the fair
head-to-head is the held-out set, where both are out-of-sample.

One thing is deliberately given away — that the labels carry roughly twelve
points of noise at ±2. Withholding it would not preserve independence, it would
just invite the rebuild to spend its budget fitting noise, which is a failure
mode this project already paid for once.
"""

from __future__ import annotations

import collections
import json
import pathlib
import random

from .repos import CORPUS_ROOT

# RootSE is withheld. It is the only set labelled by people unconnected to this
# project, which makes it the one measurement immune to the labelling bias this
# project's own sets carry, and spending it as development data would waste that
# for a second time. The rebuild develops on the in-house sets and is tested on
# RootSE and on the held-out set, neither of which it sees.
DEV_SOURCES = ("nebius", "openhands")

# One ratio for both in-house sources, so neither is measured more finely than
# the other by accident of how they were drawn. 40% leaves the OpenHands test
# side near the point where sampling error stops dominating, which the old
# 80/37 split was well short of.
DEV_FRACTION = 0.40
PARTITION_SEED = 20260818


def _eligible(steps, messages=None) -> bool:
    """The filters holdout-2 was drawn under, applied to the older sets too.

    The first-pass sets were never screened for degenerate rollouts: 18 of the
    80 OpenHands items are runs that emitted no prose or never saw two distinct
    observations. Repartitioning without this would move some of them into the
    test side, where short degenerate traces cannot be missed and inflate the
    score. nebius has none.
    """
    from .relabel import has_model_prose, learned_something

    if messages is not None and not has_model_prose(messages):
        return False
    return learned_something(steps)


def partition(pool: list[str], seed: int = PARTITION_SEED) -> tuple[list[str], list[str]]:
    """Split one source's eligible ids into (dev, test) at DEV_FRACTION.

    Development is filled from the older sets first. Both halves are equally
    fresh to the rebuild, which has seen none of them, but they are not equally
    valuable: the older sets were already spent selecting the incumbent rule,
    while the held-out set is the only pool where the incumbent is also
    out-of-sample. Spending a held-out item on development costs a paired
    comparison that an older item does not, so the older ones go first and the
    ratio comes out the same either way.
    """
    old = sorted(uid for uid in pool if not uid.startswith("h2/"))
    held = sorted(uid for uid in pool if uid.startswith("h2/"))
    rng = random.Random(seed)
    rng.shuffle(old)
    rng.shuffle(held)
    cut = round(len(pool) * DEV_FRACTION)
    dev = old[:cut]
    if len(dev) < cut:  # older sets exhausted; spill into the held-out pool
        dev += held[: cut - len(dev)]
    devset = set(dev)
    return sorted(devset), sorted(uid for uid in pool if uid not in devset)


def _dump_step(step) -> dict:
    return {
        "action": step.name,
        "target": step.target,
        "args": step.args,
        "reasoning": step.reasoning,
        "observation": step.observation,
        "writes": sorted(step.writes),
        "batch_actions": list(step.batch_names),
    }


def _nebius_rows() -> list[tuple[str, int, list]]:
    import pyarrow.parquet as pq

    from .nebius import _snapshot, to_steps

    root = CORPUS_ROOT / "labels" / "nebius"
    key = {
        json.loads(l)["packet_id"]: json.loads(l)
        for l in (root / "key.jsonl").read_text().splitlines() if l.strip()
    }
    labels = {
        json.loads(l)["packet_id"]: json.loads(l)["label"]
        for l in (root / "labels.jsonl").read_text().splitlines()
        if l.strip() and json.loads(l).get("label") is not None
    }
    wanted: dict[str, set[int]] = collections.defaultdict(set)
    for packet in labels:
        shard, index = key[packet]["bad_row"].rsplit(":", 1)
        wanted[shard].add(int(index))
    cache, snapshot = {}, _snapshot()
    for shard, indexes in wanted.items():
        column = pq.read_table(snapshot / shard, columns=["trajectory"])["trajectory"]
        for index in sorted(indexes):
            cache[f"{shard}:{index}"] = column[index].as_py()
        del column
    out = []
    for packet, label in labels.items():
        steps = to_steps(cache[key[packet]["bad_row"]])
        if steps:
            out.append((packet, label, steps))
    return out


def _openhands_rows() -> list[tuple[str, int, list]]:
    import pyarrow.parquet as pq

    from .external import strip_terminal, to_steps
    from .relabel import _openhands_snapshot

    root = CORPUS_ROOT / "labels" / "openhands"
    key = {
        json.loads(l)["packet_id"]: json.loads(l)
        for l in (root / "key.jsonl").read_text().splitlines() if l.strip()
    }
    labels = {
        json.loads(l)["packet_id"]: json.loads(l)["label"]
        for l in (root / "labels.jsonl").read_text().splitlines()
        if l.strip() and json.loads(l).get("label") is not None
    }
    wanted = {(key[p]["instance_id"], key[p]["bad_run_id"]): p for p in labels}
    found: dict[str, list] = {}
    for shard in sorted(_openhands_snapshot().glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "run_id", "messages"])
        for instance, run, messages in zip(
            table["instance_id"].to_pylist(),
            table["run_id"].to_pylist(),
            table["messages"].to_pylist(),
            strict=True,
        ):
            packet = wanted.get((instance, run))
            if packet:
                found[packet] = messages
    out = []
    for packet, messages in found.items():
        steps = strip_terminal(to_steps(messages, shell_verbs=True))
        if steps:
            out.append((packet, labels[packet], steps))
    return out


def export(destination: pathlib.Path) -> dict[str, str]:
    """Write the development file. Returns the opaque-id map, which stays here."""
    rows: list[tuple[str, str, int, list]] = []
    for packet, label, steps in _nebius_rows():
        rows.append(("A", f"nebius/{packet}", label, steps))
    for packet, label, steps in _openhands_rows():
        rows.append(("B", f"openhands/{packet}", label, steps))

    counter: collections.Counter = collections.Counter()
    mapping: dict[str, str] = {}
    lines = []
    for source, real_id, label, steps in rows:
        counter[source] += 1
        opaque = f"{source}{counter[source]:03d}"
        mapping[opaque] = real_id
        lines.append(json.dumps({
            "id": opaque, "source": source, "label": label,
            "steps": [_dump_step(s) for s in steps],
        }))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mapping


if __name__ == "__main__":
    import sys

    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cleanroom/data/train.jsonl")
    mapping = export(target)
    map_path = CORPUS_ROOT / "alignment" / "cleanroom-id-map.json"
    map_path.write_text(json.dumps(mapping, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"exported {len(mapping)} trajectories to {target}")
    print(f"id map (stays in this repo) -> {map_path}")
