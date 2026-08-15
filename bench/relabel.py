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

from tracewake.align import Step

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


def has_model_prose(messages: list[dict]) -> bool:
    """Did the model say anything at all across the rollout?

    36% of OpenHands failing rollouts emit no prose in any assistant turn: a
    handful of identical tool calls against an empty directory listing, no
    patch, and `empty_generation` from the grader. There is no point of no
    return in one of those, because there is no decision — the run failed
    before the agent did anything a rule could read. They are excluded here
    rather than labelled, and the exclusion is fixed before any label exists.

    nebius shows none of this: zero of 660 sampled rollouts lack prose.
    """
    return any((m.get("content") or "").strip() for m in messages if m.get("role") == "assistant")


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
        table = pq.read_table(shard, columns=["instance_id", "resolved", "run_id", "messages"])
        for instance, resolved, run, messages in zip(
            table["instance_id"].to_pylist(),
            table["resolved"].to_pylist(),
            table["run_id"].to_pylist(),
            table["messages"].to_pylist(),
            strict=True,
        ):
            if resolved or instance in excluded or model_of_run_id(run) != model:
                continue
            if not has_model_prose(messages):
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


# Long enough that the decisive detail is rarely cut, short enough that a packet
# stays readable. Observations are held tighter than the rest: they are included
# so a step's effect is visible — whether an edit applied, whether a test errored
# — not so the label can be read off a traceback, which would answer where the
# failure became evident rather than where it became certain.
ARG_CHARS = 400
REASON_CHARS = 600
OBSERVATION_CHARS = 300

DEFINITION = """\
Operational definition: the 1-based index of the earliest step after which no
later step could plausibly have recovered this run, without undoing work already
done or outside intervention.

Not the first mistake — a wrong turn the run notices and fixes is not it.
Not where failure became evident — evidence arrives later than commitment.
Not the last step by default.

If the run was never on a recoverable path, answer 1. If it stayed recoverable
to the very end, answer the last step. A step with no action can be the answer:
if the decisive commitment is made in reasoning and later steps merely execute
it, name the reasoning step. If two adjacent steps both qualify, take the
earlier.

Exclusions, in place of an integer: E1 truncated or malformed, E2 failed for
reasons outside the run's control, E3 appears to have solved it, E4 no judgment
reachable after reading the whole trajectory.
"""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]} …[+{len(text) - limit} chars]"


def load_steps(draw: Draw, rows: dict[str, object] | None = None) -> list[Step]:
    """The failing trajectory, extracted exactly as its source is scored.

    OpenHands strips the trailing `finish` and SWE-agent does not; the two
    adapters differ here, and a label indexes the steps a method will see.
    """
    if draw.source == "nebius":
        from .nebius import to_steps

        trajectory = rows[row_key(draw)] if rows is not None else _nebius_row(draw.row)
        return to_steps(trajectory)
    from .external import strip_terminal, to_steps

    messages = rows[row_key(draw)] if rows is not None else _openhands_row(draw)
    return strip_terminal(to_steps(messages, shell_verbs=True))


def row_key(draw: Draw) -> str:
    """What identifies one rollout inside its source.

    An OpenHands run id names the sampling configuration, not the rollout —
    6,055 rollouts share nine of them — so it identifies a trajectory only
    together with its instance. A nebius row is already unique.
    """
    if draw.source == "nebius":
        return draw.row
    return f"{draw.instance_id}|{draw.row}"


def _nebius_row(row: str):
    import pyarrow.parquet as pq

    from .nebius import _snapshot

    shard, index = row.rsplit(":", 1)
    return pq.read_table(_snapshot() / shard, columns=["trajectory"])["trajectory"][int(index)].as_py()


def _openhands_row(draw: Draw):
    import pyarrow.parquet as pq

    for shard in sorted(_openhands_snapshot().glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "run_id", "messages"])
        for instance, run, messages in zip(
            table["instance_id"].to_pylist(),
            table["run_id"].to_pylist(),
            table["messages"].to_pylist(),
            strict=True,
        ):
            if instance == draw.instance_id and run == draw.row:
                return messages
    raise KeyError(f"{draw.packet_id}: no rollout {draw.row} for {draw.instance_id}")


def render_packet(draw: Draw, steps: list[Step]) -> str:
    """One trajectory as the labeller sees it.

    The instance, the model and the source are withheld: they name the bug and
    the scaffold, and neither belongs in a judgment about this run's own steps.
    """
    lines = [f"# Packet {draw.packet_id}", "", DEFINITION, "Label: ________", "Confident: ________", "", f"## FAILING RUN  ({len(steps)} steps)", ""]
    for index, step in enumerate(steps, start=1):
        head = f"  {index:>3}. {step.name}"
        if step.target:
            head += f"  → {step.target}"
        lines.append(head)
        if step.args:
            lines.append(f"       args: {_clip(json.dumps(step.args, sort_keys=True), ARG_CHARS)}")
        if step.reasoning:
            lines.append(f"       reason: {_clip(step.reasoning, REASON_CHARS)}")
        if step.observation:
            lines.append(f"       saw: {_clip(step.observation, OBSERVATION_CHARS)}")
        lines.append("")
    return "\n".join(lines)


def export(draws: list[Draw], name: str) -> Path:
    """Write packets and the key that maps them back, into separate files.

    The key names the instance and, for calibration, the packet whose label is
    being reproduced. It stays closed until every packet in the set is labelled.
    """
    root = LABEL_ROOT / name
    (root / "packets").mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Draw]] = defaultdict(list)
    for draw in draws:
        grouped[draw.source].append(draw)
    for source, items in grouped.items():
        rows = _bulk_rows(source, items)
        for draw in items:
            steps = load_steps(draw, rows)
            (root / "packets" / f"{draw.packet_id}.md").write_text(
                render_packet(draw, steps), encoding="utf-8"
            )
    key = [
        {
            "packet_id": d.packet_id,
            "source": d.source,
            "instance_id": d.instance_id,
            "model": d.model,
            "row": d.row,
            **({"origin_packet": d.origin_packet} if d.origin_packet else {}),
        }
        for d in sorted(draws, key=lambda d: d.packet_id)
    ]
    (root / "key.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in key) + "\n", encoding="utf-8"
    )
    return root


def _bulk_rows(source: str, draws: list[Draw]) -> dict[str, object]:
    """Every trajectory a source needs, read one shard at a time."""
    import pyarrow.parquet as pq

    if source == "nebius":
        from .nebius import _snapshot

        wanted: dict[str, set[int]] = defaultdict(set)
        for draw in draws:
            shard, index = draw.row.rsplit(":", 1)
            wanted[shard].add(int(index))
        out: dict[str, object] = {}
        snapshot = _snapshot()
        for shard, indices in sorted(wanted.items()):
            column = pq.read_table(snapshot / shard, columns=["trajectory"])["trajectory"]
            for index in sorted(indices):
                out[f"{shard}:{index}"] = column[index].as_py()
            del column
        return out
    wanted_runs = {(d.instance_id, d.row): row_key(d) for d in draws}
    out = {}
    for shard in sorted(_openhands_snapshot().glob("*.parquet")):
        table = pq.read_table(shard, columns=["instance_id", "run_id", "messages"])
        for instance, run, messages in zip(
            table["instance_id"].to_pylist(),
            table["run_id"].to_pylist(),
            table["messages"].to_pylist(),
            strict=True,
        ):
            key = wanted_runs.get((instance, run))
            if key is not None:
                out[key] = messages
    return out


def summarize(draws: list[Draw]) -> str:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for item in draws:
        counts[(item.source, item.model)] += 1
    lines = [f"{len(draws)} items, {len({d.instance_id for d in draws})} distinct instances"]
    for (source, model), count in sorted(counts.items()):
        lines.append(f"  {source:<10} {model:<24} {count}")
    return "\n".join(lines)
