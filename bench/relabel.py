"""The second labelling pass: a calibration set and a fresh held-out set.

The first pass exhausted every label this project had. `earliest_bound` was
selected while looking at all four sets, the two checks on that selection were
each scored once, and the corpus is closed. Nothing labelled remains that can
give an unbiased estimate of anything built from here on.

What is not exhausted is the data. Requiring a passing reference run for each
failure is what held the usable pool to a few hundred pairs; a single-trace rule
needs no reference, and without that constraint the sources hold roughly 72,000
failing trajectories over about 5,800 instances. Labels, not trajectories, are
the binding constraint, so this module spends a fixed budget of them where they
buy the most.

Two sets, drawn together so they cannot overlap:

  * `test` — 140 fresh trajectories, instance-disjoint from everything already
    labelled, stratified over source and model so the result says something
    about generalization rather than about one scaffold.
  * `calibration` — 60 trajectories that already carry a label from the first
    pass, re-rendered under this protocol and re-labelled without the old label
    visible. Agreement between the two is the only measurement that says whether
    these labels mean the same thing the earlier ones did, and disagreement
    bounds what any rule can score: labels that differ by more than the scoring
    tolerance put a ceiling on accuracy that no method can pass.

Selection consults no method output, and both draws are seeded and instance
disjoint. One trajectory per instance: several rollouts of one bug are not
independent evidence, and the same instance appears in more than one source.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from .repos import CORPUS_ROOT

# Fixed before the draw and never changed. A re-draw under a new seed after
# seeing any result would turn this into a search over samples.
TEST_SEED = 20260815
CALIBRATION_SEED = 20260816

LABEL_ROOT = CORPUS_ROOT / "labels"
NEBIUS_DATASET = "nebius/SWE-agent-trajectories"
OPENHANDS_DATASET = "SWE-Gym/OpenHands-Sampled-Trajectories"

# Allocated scarcest first, so a stratum with few eligible instances is not
# starved by one that could have drawn from anywhere.
TEST_STRATA: tuple[tuple[str, str, int], ...] = (
    ("nebius", "swe-agent-llama-405b", 25),
    ("nebius", "swe-agent-llama-8b", 25),
    ("nebius", "swe-agent-llama-70b", 50),
    ("openhands", "gpt-4o-2024-08-06", 40),
)

# Proportional to how many labels each existing set holds, so agreement is
# measured against all three rather than against whichever is largest.
CALIBRATION_QUOTA: tuple[tuple[str, int], ...] = (
    ("external", 32),
    ("nebius", 16),
    ("nebius-holdout", 12),
)


@dataclass(frozen=True)
class Draw:
    """One trajectory selected for labelling, named by where it came from."""

    packet_id: str
    source: str
    instance_id: str
    model: str
    # nebius rows are "shard:index"; OpenHands rows are a run id.
    row: str
    # Set only for calibration items, where a first-pass label already exists.
    origin_packet: str | None = None


def _keyed_instances(name: str) -> list[dict]:
    path = LABEL_ROOT / name / "key.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def excluded_instances(rootse_ids: list[str]) -> set[str]:
    """Every instance already labelled, from any source.

    Splitting by rollout rather than by instance would put the same underlying
    bug on both sides: the sources share instance identifiers, and one instance
    carries many rollouts.
    """
    excluded = {row["instance_id"] for name, _ in CALIBRATION_QUOTA for row in _keyed_instances(name)}
    return excluded | set(rootse_ids)


def _nebius_candidates(excluded: set[str]) -> dict[str, dict[str, list[str]]]:
    """Eligible failing rows per model, grouped by instance."""
    import pyarrow.parquet as pq

    from .nebius import _snapshot

    snapshot = _snapshot()
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for shard in sorted(snapshot.glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "model_name", "target"])
        instances = table["instance_id"].to_pylist()
        models = table["model_name"].to_pylist()
        targets = table["target"].to_pylist()
        for index, (instance, model, resolved) in enumerate(zip(instances, models, targets, strict=True)):
            if resolved or instance in excluded:
                continue
            grouped[model][instance].append(f"{shard.name}:{index}")
    return {model: dict(byinstance) for model, byinstance in grouped.items()}


def _openhands_snapshot() -> Path:
    """The downloaded shards, wherever the hub cache put them."""
    cache = Path.home() / ".cache/huggingface/hub"
    hits = sorted(cache.glob("datasets--SWE-Gym--OpenHands-Sampled-Trajectories/snapshots/*/data"))
    if not hits:
        raise RuntimeError(f"{OPENHANDS_DATASET} is not in the local hub cache")
    return hits[-1]


def _openhands_candidates(excluded: set[str], model: str) -> dict[str, list[str]]:
    """Eligible failing run ids for one model, grouped by instance.

    Read from the shards rather than through `datasets`, which this project does
    not depend on. The model is not a column; it is the prefix of the run id,
    decoded the same way the rest of the OpenHands adapter decodes it.
    """
    import pyarrow.parquet as pq

    from .external import model_of_run_id

    grouped: dict[str, list[str]] = defaultdict(list)
    for shard in sorted(_openhands_snapshot().glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "resolved", "run_id"])
        for instance, resolved, run in zip(
            table["instance_id"].to_pylist(),
            table["resolved"].to_pylist(),
            table["run_id"].to_pylist(),
            strict=True,
        ):
            if resolved or instance in excluded or model_of_run_id(run) != model:
                continue
            grouped[instance].append(run)
    return dict(grouped)


def draw_test(rootse_ids: list[str]) -> list[Draw]:
    """The held-out set: stratified, instance-disjoint, one rollout per instance.

    Rollouts within an instance are chosen uniformly rather than by any property
    of the trajectory. Picking the longest or the shortest would shift the length
    distribution, and length is the strongest single correlate of whether the
    current rule lands.
    """
    excluded = set(excluded_instances(rootse_ids))
    rng = random.Random(TEST_SEED)
    nebius = _nebius_candidates(excluded)
    pools = {
        (source, model): (
            nebius.get(model, {}) if source == "nebius" else _openhands_candidates(excluded, model)
        )
        for source, model, _ in TEST_STRATA
    }
    drawn: list[Draw] = []
    for source, model, quota in TEST_STRATA:
        pool = pools[(source, model)]
        available = sorted(instance for instance in pool if instance not in excluded)
        if len(available) < quota:
            raise RuntimeError(
                f"{source}/{model} has {len(available)} eligible instances for a quota of {quota}"
            )
        for instance in rng.sample(available, quota):
            rows = sorted(pool[instance])
            drawn.append(
                Draw(
                    packet_id="",
                    source=source,
                    instance_id=instance,
                    model=model,
                    row=rng.choice(rows),
                )
            )
            # Disjoint across strata too: one instance appears under several
            # models, and the same bug twice is not two measurements.
            excluded.add(instance)
    rng.shuffle(drawn)
    return [replace(item, packet_id=f"T{index:03d}") for index, item in enumerate(drawn, start=1)]


def draw_calibration() -> list[Draw]:
    """The agreement set: first-pass items re-rendered under this protocol.

    The failing side only. These carry a label already, and it stays unread until
    every one of them has been labelled again.
    """
    rng = random.Random(CALIBRATION_SEED)
    drawn: list[Draw] = []
    for name, quota in CALIBRATION_QUOTA:
        rows = _keyed_instances(name)
        if len(rows) < quota:
            raise RuntimeError(f"{name} holds {len(rows)} packets for a quota of {quota}")
        for row in rng.sample(sorted(rows, key=lambda r: r["packet_id"]), quota):
            source = "openhands" if name == "external" else "nebius"
            drawn.append(
                Draw(
                    packet_id="",
                    source=source,
                    instance_id=row["instance_id"],
                    model=row["model"],
                    row=row["bad_row"] if source == "nebius" else row["bad_run_id"],
                    origin_packet=f"{name}/{row['packet_id']}",
                )
            )
    rng.shuffle(drawn)
    return [replace(item, packet_id=f"C{index:03d}") for index, item in enumerate(drawn, start=1)]


def summarize(draws: list[Draw]) -> str:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for item in draws:
        counts[(item.source, item.model)] += 1
    lines = [f"{len(draws)} items, {len({d.instance_id for d in draws})} distinct instances"]
    for (source, model), count in sorted(counts.items()):
        lines.append(f"  {source:<10} {model:<24} {count}")
    return "\n".join(lines)
