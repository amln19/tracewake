"""The reserved transfer set: same-model mixed-outcome pairs from SWE-agent.

Every other labelled set this project has is spent. Both OpenHands halves were
used for design and selection, and RootSE has been scored twice. `nebius`
(80,036 SWE-agent rollouts, 16.7% resolved, several per instance) is the only
remaining source that can supply an untouched held-out evaluation, and it is
untouched precisely because nothing here has read its labels — it has none.

That is the point and the cost. It yields pairs cheaply and labels not at all,
so a held-out number required a labelling pass first. This module builds the
pool, exports the packets for that pass, and — now that the pass is done and
the set is spent — scores against the result.

The labels were produced from the packets alone, with no method's prediction
visible, which is what makes the comparison between rules fair. It does not
make them independent of the rules: whoever wrote the rule also decided what
"the point of no return" means here, and the two cannot be fully separated.
`contracts/divergence.md` states the limit that follows.

Pair choice never consults a method's output. It mirrors the rule used for the
corpus and the OpenHands set: maximise the shorter trajectory, then the longer,
then break ties on row identity so a reload picks the same pair.
"""

from __future__ import annotations

import json
import random
import textwrap
from dataclasses import dataclass
from pathlib import Path

from tracewake.align import Step

from .repos import CORPUS_ROOT

DATASET = "nebius/SWE-agent-trajectories"
LABEL_ROOT = CORPUS_ROOT / "labels" / "nebius"
SELECT_SEED = 20260812
MAX_RATIO = 4.0
REASON_CHARS = 280
ARG_CHARS = 160


@dataclass(frozen=True)
class NebiusPair:
    instance_id: str
    model: str
    good: tuple[Step, ...]
    bad: tuple[Step, ...]
    good_row: int
    bad_row: int


# SWE-agent's Agent-Computer Interface, not plain shell. `open` and `goto`
# navigate, `edit` writes to whatever file the ACI currently has open — which
# is why the open file has to be tracked to know what an edit touched.
ACI_OPEN = ("open", "create")
ACI_EDIT = ("edit", "insert", "append", "submit_edit")
# The model's turn is tagged `ai` in this dump, not `assistant`.
AGENT_ROLES = ("ai", "assistant")


def _fenced_command(text: str) -> str:
    if "```" not in text:
        return ""
    parts = text.split("```")
    if len(parts) < 3:
        return ""
    command = parts[-2].strip()
    # A fenced block may open with a bare language tag on its own line.
    head, _, rest = command.partition("\n")
    if rest and head.isalpha() and len(head.split()) == 1:
        command = rest.strip()
    return command


def to_steps(trajectory) -> list[Step]:
    """SWE-agent turns as alignment steps.

    The action is the fenced command block of the model's turn and the prose
    before it is the reasoning. `bench.rootse.decode_action` handles the shell
    forms; the ACI's stateful editor needs the extra tracking below, because
    `edit` names no path — it writes to whatever `open` last selected.
    """
    from .rootse import decode_action

    steps: list[Step] = []
    open_file = ""
    for message in trajectory:
        if not isinstance(message, dict) or message.get("role") not in AGENT_ROLES:
            continue
        text = message.get("text") or message.get("content") or ""
        if not isinstance(text, str):
            continue
        command = _fenced_command(text)
        if not command:
            continue
        verb = (command.split() or [""])[0].strip("\"'")
        argument = command.split()[1].strip("\"'") if len(command.split()) > 1 else ""

        if verb in ACI_OPEN:
            open_file = argument or open_file
            decoded = (verb, {"command": command, "path": open_file}, open_file,
                       {open_file} if verb == "create" and open_file else set())
        elif verb in ACI_EDIT:
            # Writes to the currently open file, which the command never names.
            decoded = (verb, {"command": command, "path": open_file}, open_file,
                       {open_file} if open_file else set())
        else:
            decoded = decode_action(command)
            if decoded is None:
                continue

        name, args, target, writes = decoded
        steps.append(
            Step(
                name=name,
                args=args,
                target=target,
                reasoning=" ".join(text.split("```")[0].split()),
                writes=frozenset(writes),
            )
        )
    return steps


def build_pairs(rows) -> list[NebiusPair]:
    """One pair per (instance, model) group whose rollouts disagree on outcome.

    `rows` is an iterable of mappings with instance_id, model_name, target and
    trajectory — the dataset's own columns — so this is testable without the
    1.1GB download.
    """
    from collections import defaultdict

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[(row["instance_id"], row["model_name"])].append(
            {**row, "_row": index}
        )

    pairs: list[NebiusPair] = []
    for (instance, model), group in sorted(grouped.items()):
        wins = [r for r in group if r["target"]]
        losses = [r for r in group if not r["target"]]
        if not wins or not losses:
            continue
        best = None
        for win in wins:
            good = to_steps(win["trajectory"])
            if not good:
                continue
            for loss in losses:
                bad = to_steps(loss["trajectory"])
                if not bad:
                    continue
                if max(len(good), len(bad)) / min(len(good), len(bad)) > MAX_RATIO:
                    continue
                key = (
                    min(len(good), len(bad)),
                    max(len(good), len(bad)),
                    win["_row"],
                    loss["_row"],
                )
                if best is None or key > best[0]:
                    best = (key, good, bad, win["_row"], loss["_row"])
        if best is not None:
            _, good, bad, win_row, loss_row = best
            pairs.append(
                NebiusPair(instance, model, tuple(good), tuple(bad), win_row, loss_row)
            )
    return pairs


# ---------------------------------------------------------------------------
# blind labelling export
# ---------------------------------------------------------------------------


def _render_side(title: str, steps) -> str:
    lines = [f"## {title}  ({len(steps)} steps)", ""]
    for i, step in enumerate(steps, start=1):
        head = f"{i:>3}. {step.name}"
        if step.target:
            head += f"  → {step.target}"
        lines.append(head)
        args = json.dumps(step.args, sort_keys=True)
        lines.append(f"     args: {args[:ARG_CHARS]}{'…' if len(args) > ARG_CHARS else ''}")
        if step.reasoning:
            lines.append(
                textwrap.fill(
                    step.reasoning[:REASON_CHARS],
                    width=88,
                    initial_indent="     reason: ",
                    subsequent_indent="             ",
                )
            )
        lines.append("")
    return "\n".join(lines)


def _render_packet(packet_id: str, pair: NebiusPair) -> str:
    # Wording is fixed to the definition in the plan. The earlier sets were
    # labelled to two different instructions and their results cannot be
    # pooled; this pass must not add a third.
    return "\n".join(
        [
            f"# Packet {packet_id}",
            "",
            "Operational definition: the earliest FAILURE-side step after which no",
            "subsequent step could plausibly recover the run. Index steps on the",
            "FAILURE side. If the run was never on a recoverable path, answer 1. If it",
            "was only doomed at the very end, answer the last step. One integer.",
            "",
            "Label: ________",
            "",
            _render_side("SUCCESS (resolved the issue)", pair.good),
            _render_side("FAILURE (did not resolve the issue)", pair.bad),
        ]
    )


def export_packets(
    pairs,
    *,
    n: int = 40,
    dest: Path = LABEL_ROOT,
    seed: int = SELECT_SEED,
) -> Path:
    """Blind packets for a labelling pass. The key stays closed until scoring."""
    ordered = list(pairs)
    rng = random.Random(seed)
    rng.shuffle(ordered)
    selected = ordered[:n]
    if not selected:
        raise RuntimeError("no nebius pairs to export; build the pool first")

    packets = dest / "packets"
    packets.mkdir(parents=True, exist_ok=True)
    for old in packets.glob("N*.md"):
        old.unlink()

    key_rows, sheet_rows = [], []
    for i, pair in enumerate(selected, start=1):
        packet_id = f"N{i:02d}"
        (packets / f"{packet_id}.md").write_text(
            _render_packet(packet_id, pair), encoding="utf-8"
        )
        key_rows.append(
            {
                "packet_id": packet_id,
                "instance_id": pair.instance_id,
                "model": pair.model,
                "good_row": pair.good_row,
                "bad_row": pair.bad_row,
                "good_steps": len(pair.good),
                "bad_steps": len(pair.bad),
            }
        )
        sheet_rows.append({"packet_id": packet_id, "label": None, "note": ""})

    (dest / "key.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in key_rows), encoding="utf-8"
    )
    sheet = dest / "labels.jsonl"
    if not sheet.exists():
        sheet.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in sheet_rows),
            encoding="utf-8",
        )
    _write_readme(dest, len(key_rows), seed)
    return dest


def _write_readme(dest: Path, pairs: int, seed: int) -> None:
    (dest / "README.txt").write_text(
        "\n".join(
            [
                f"Blind labelling packets from {DATASET}.",
                "",
                "  packets/     one markdown file per pair. Do not open key.jsonl",
                "               while labelling.",
                "  key.jsonl    packet_id -> instance / model / dataset rows.",
                "  labels.jsonl fill `label` with the 1-based FAILURE step.",
                "",
                f"{pairs} pairs, seed {seed}.",
                "",
                "This was the last untouched transfer set; it is now spent. Labels were",
                "written from the packets alone, with no method's prediction visible.",
                "",
            ]
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _snapshot() -> Path:
    """The downloaded parquet shards, wherever `datasets` put them."""
    cache = Path.home() / ".cache/huggingface/hub"
    hits = sorted(
        cache.glob("datasets--nebius--SWE-agent-trajectories/snapshots/*/data")
    )
    if not hits:
        raise RuntimeError(
            f"{DATASET} is not in the local hub cache; re-download it before scoring"
        )
    return hits[-1]


def _load_rows(key: dict, packets, snapshot: Path) -> dict[str, dict]:
    """Trajectories for the rows the key names, one parquet shard at a time."""
    from collections import defaultdict

    import pyarrow.parquet as pq

    wanted: dict[str, set[int]] = defaultdict(set)
    for pid in packets:
        for side in ("good_row", "bad_row"):
            shard, index = key[pid][side].rsplit(":", 1)
            wanted[shard].add(int(index))

    trajectories: dict[str, dict] = {}
    for shard, indices in sorted(wanted.items()):
        column = pq.read_table(snapshot / shard, columns=["trajectory"])["trajectory"]
        for index in sorted(indices):
            trajectories[f"{shard}:{index}"] = column[index].as_py()
        del column
    return trajectories


def score_packets(root: Path = LABEL_ROOT, *, tolerance: int = 2) -> dict:
    """Every rule's within-tolerance accuracy against the nebius labels.

    Reports `first_commitment` alongside `earliest_bound` because the
    registered prediction was about whether a reference run helps here; see
    `contracts/divergence.md`.
    """
    from tracewake.align import LexicalEmbedder, align, divergence_step
    from tracewake.diverge import earliest_bound, first_commitment

    key = {
        r["packet_id"]: r
        for r in map(json.loads, (root / "key.jsonl").read_text().splitlines())
    }
    labels = {
        r["packet_id"]: r["label"]
        for r in map(json.loads, (root / "labels.jsonl").read_text().splitlines())
        if r["label"] is not None
    }
    if not labels:
        raise RuntimeError(f"no labels filled in {root / 'labels.jsonl'}")

    embed = LexicalEmbedder()
    rows = _load_rows(key, sorted(labels), _snapshot())
    hits: dict[str, list[bool]] = {}

    for pid in sorted(labels):
        good = to_steps(rows[key[pid]["good_row"]])
        bad = to_steps(rows[key[pid]["bad_row"]])
        if (len(good), len(bad)) != (key[pid]["good_steps"], key[pid]["bad_steps"]):
            raise RuntimeError(f"{pid}: trajectory no longer matches the exported packet")

        _, alignment, _ = align(good, bad, embed=embed)
        lexical = divergence_step(alignment, good, bad)
        commitment = first_commitment(bad)
        predictions = {
            "earliest_bound": earliest_bound(bad),
            "first_commitment": commitment if commitment is not None else len(bad),
            "lexical-v1": lexical if lexical is not None else len(bad),
            # Fitted on OpenHands development data; carried over unchanged.
            "dev-constant-10": min(10, len(bad)),
            "dev-proportional-.66": max(1, round(0.66 * len(bad))),
        }
        for rule, predicted in predictions.items():
            hits.setdefault(rule, []).append(abs(predicted - labels[pid]) <= tolerance)

    return {rule: (sum(h), len(h)) for rule, h in sorted(hits.items())}
