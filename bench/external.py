"""Alignment against trajectories this project did not record.

The aligner consumes step sequences and nothing else — no cassettes, no
provenance, no replay — so trajectories somebody else published can be read
directly.

Sources tried (see corpus/alignment/external_scout.json):

- `SWE-bench/SWE-smith-trajectories` — refused: same task id under two prompts
  (fix-source vs bug-report); same-instruction pairing yields zero pairs.
- `SWE-Gym/OpenHands-Sampled-Trajectories` — chosen: same OpenHands scaffold,
  one model, shared PR instruction, mixed resolve under the corpus length gate.
- Leaderboard `trajs/` — downloadable but typically cross-model / per-submission
  formats; kept as a secondary route.
- Who&When — different task (single-fail decisive step), not good/bad pairs.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import textwrap
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracewake.align import Step

from .repos import CORPUS_ROOT, corpus_metadata_path

# The same split carries two shapes. Some rollouts record structured
# `tool_calls`; others leave the call inline in the assistant's prose. Reading
# only the structured one silently drops whole trajectories rather than failing.
_FUNCTION_BLOCK = re.compile(r"<function=([\w.\-]+)>(.*?)</function>", re.DOTALL)
_PARAMETER = re.compile(r"<parameter=([\w.\-]+)>(.*?)</parameter>", re.DOTALL)

# SWE-agent drives everything through a handful of functions, and `bash` is a
# catch-all whose real action is the first word of the command. Left as one
# name, most steps would be indistinguishable and the alignment would be
# comparing "bash" to "bash" all the way down.
_SHELL = "bash"
_EDITOR = "str_replace_editor"
_EDIT_COMMANDS = frozenset({"str_replace", "create", "insert"})
_OPENHANDS_BASH = "execute_bash"
_TERMINAL_ACTIONS = frozenset({"submit", "finish"})

OPENHANDS_DATASET = "SWE-Gym/OpenHands-Sampled-Trajectories"
OPENHANDS_SPLIT = "train.raw"
SELECT_SEED = 20260805
EXTERNAL_LABEL_ROOT = CORPUS_ROOT / "labels" / "external"
EXTERNAL_PRED = CORPUS_ROOT / "alignment" / "predictions-external.jsonl"
SCOUT_PATH = CORPUS_ROOT / "alignment" / "external_scout.json"

REASON_CHARS = 280
ARG_CHARS = 160


@dataclass(frozen=True)
class ExternalPair:
    instance_id: str
    model: str
    good: tuple[Step, ...]
    bad: tuple[Step, ...]
    good_run_id: str = ""
    bad_run_id: str = ""

    @property
    def length_ratio(self) -> float:
        long, short = max(len(self.good), len(self.bad)), min(len(self.good), len(self.bad))
        return long / short if short else float("inf")


def _looks_like_path(token: str) -> bool:
    return "/" in token and not token.startswith("-")


def _shell_step(command: str) -> tuple[str, dict[str, Any]]:
    """A shell command as (verb, args), with the first path it names as target."""
    tokens = command.split()
    verb = tokens[0] if tokens else _SHELL
    args: dict[str, Any] = {"command": command}
    path = next((t for t in tokens[1:] if _looks_like_path(t)), "")
    if path:
        args["path"] = path.rstrip(";|&")
    return verb, args


def _structured_calls(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            # A malformed argument blob is an action the agent took and the
            # environment rejected; dropping it would shorten the trace.
            args = {"command": str(raw)}
        out.append((function.get("name") or _SHELL, args))
    return out


def _inline_calls(content: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, {k: v.strip() for k, v in _PARAMETER.findall(body)})
        for name, body in _FUNCTION_BLOCK.findall(content)
    ]


def instruction_kind(messages: Sequence[dict[str, Any]]) -> str:
    """Which job the rollout was given.

    The same `instance_id` is issued under two different prompts — fix the
    source, or write a bug report — and `resolved` is graded on the tests, which
    a bug-report run can never make pass because it never edits source. Pairing
    across the two compares runs asked to do different things, and every step of
    the resulting "divergence" is an artifact of the instruction.
    """
    users = [m for m in messages if m.get("role") == "user"]
    text = _text_of(users[0].get("content")) if users else ""
    return "bug_report" if "example_bug_report" in text else "fix_source"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _normalize_editor(
    name: str, args: dict[str, Any], changed: set[tuple[str, str]]
) -> tuple[str, dict[str, Any]]:
    if name in (_EDITOR, "str_replace_editor") or name.endswith("str_replace_editor"):
        command = str(args.get("command", "view"))
        if command in _EDIT_COMMANDS and args.get("path"):
            changed.add((str(args["path"]), command))
        return f"{_EDITOR}.{command}", args
    return name, args


def to_steps(
    messages: Sequence[dict[str, Any]], *, shell_verbs: bool = True
) -> list[Step]:
    """Assistant turns that took an action, as the alignment sequence.

    `shell_verbs` splits `bash` / `execute_bash` into the command's first word.
    It changes what counts as the same action, so both settings are reported
    rather than one being assumed.
    """
    steps: list[Step] = []
    changed: set[tuple[str, str]] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = _text_of(message.get("content"))
        calls = _structured_calls(message) or _inline_calls(content)
        if not calls:
            continue
        reasoning = (message.get("thought") or "").strip()
        if not reasoning:
            # Inline form keeps the reasoning as the prose before the call.
            reasoning = content.split("<function=")[0].strip()
        names: list[str] = []
        targets: list[str] = []
        merged: dict[str, Any] = {}
        written: set[str] = set()
        for name, args in calls:
            if name in (_SHELL, _OPENHANDS_BASH):
                verb, args = _shell_step(str(args.get("command", "")))
                name = verb if shell_verbs else name
            else:
                name, args = _normalize_editor(name, args, changed)
                # Not a `changed` delta: the set is keyed by (path, command), so
                # a second edit to the same path adds nothing to it while still
                # being a write.
                command = name.rpartition(".")[2]
                if name.startswith(f"{_EDITOR}.") and command in _EDIT_COMMANDS:
                    if args.get("path"):
                        written.add(str(args["path"]))
            names.append(name)
            targets.append(str(args.get("path", "")))
            merged.update(args)
        steps.append(
            Step(
                name=names[0],
                args=merged,
                target=targets[0],
                reasoning=reasoning,
                changed_files=frozenset(changed),
                batch_names=tuple(names) if len(names) > 1 else (),
                batch_targets=tuple(targets) if len(targets) > 1 else (),
                writes=frozenset(written),
            )
        )
    return steps


def strip_terminal(steps: Sequence[Step]) -> list[Step]:
    """Drop trailing harness terminals every rollout ends on.

    The divergence rule walks backward to the last column that agrees. A
    terminal action both runs always take is always that column, so the rule
    reports "they re-align through the end" for every pair and the index
    collapses onto the last step. SWE-agent uses `submit`; OpenHands uses
    `finish`.
    """
    out = list(steps)
    while out and out[-1].name in _TERMINAL_ACTIONS:
        out.pop()
    return out


def load_pairs(
    parquet: Path,
    *,
    model: str | None = None,
    max_ratio: float = 4.0,
    shell_verbs: bool = True,
) -> list[ExternalPair]:
    """One good/bad pair per mixed-outcome task, from a single model.

    Pair choice mirrors the corpus rule: maximise the shorter trajectory, then
    the longer, so the pair with the most to align wins and nothing about a
    method's score enters the selection.
    """
    import pyarrow.parquet as pq

    table = pq.read_table(parquet)
    columns = {name: table.column(name).to_pylist() for name in
               ("instance_id", "resolved", "model", "messages")}
    grouped: dict[tuple[str, str, str], list[tuple[bool, str]]] = {}
    for instance, resolved, name, messages in zip(
        columns["instance_id"], columns["resolved"], columns["model"], columns["messages"],
        strict=True,
    ):
        if model is not None and name != model:
            continue
        kind = instruction_kind(json.loads(messages))
        grouped.setdefault((instance, name, kind), []).append((bool(resolved), messages))

    pairs: list[ExternalPair] = []
    for (instance, name, _kind), rollouts in sorted(grouped.items()):
        wins = [m for ok, m in rollouts if ok]
        losses = [m for ok, m in rollouts if not ok]
        if not wins or not losses:
            continue
        candidates = [
            (
                strip_terminal(to_steps(json.loads(w), shell_verbs=shell_verbs)),
                strip_terminal(to_steps(json.loads(l), shell_verbs=shell_verbs)),
            )
            for w in wins
            for l in losses
        ]
        candidates = [(g, b) for g, b in candidates if g and b]
        if not candidates:
            continue
        good, bad = max(candidates, key=lambda gb: (min(len(gb[0]), len(gb[1])),
                                                    max(len(gb[0]), len(gb[1]))))
        pair = ExternalPair(instance, name, tuple(good), tuple(bad))
        if pair.length_ratio <= max_ratio:
            pairs.append(pair)
    return pairs


def model_of_run_id(run_id: str) -> str:
    """OpenHands run ids embed the model before `_maxiter_`."""
    if "_maxiter_" in run_id:
        return run_id.split("_maxiter_", 1)[0]
    return run_id.split("_", 1)[0]


def _require_datasets():
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "reading OpenHands trajectories needs the `datasets` package "
            "(pip install datasets). It is kept out of the tracewake wheel — only "
            "this scout/eval path uses it."
        ) from exc
    return load_dataset


def iter_openhands_rows(
    dataset: str = OPENHANDS_DATASET,
    split: str = OPENHANDS_SPLIT,
) -> Iterator[dict[str, Any]]:
    load_dataset = _require_datasets()
    ds = load_dataset(dataset, split=split)
    for row in ds:
        yield {
            "instance_id": row["instance_id"],
            "run_id": row["run_id"],
            "resolved": bool(row["resolved"]),
            "messages": row["messages"],
            "model": model_of_run_id(row["run_id"]),
        }


def load_openhands_pairs(
    *,
    dataset: str = OPENHANDS_DATASET,
    split: str = OPENHANDS_SPLIT,
    model: str | None = "gpt-4o-2024-08-06",
    max_ratio: float = 4.0,
    shell_verbs: bool = True,
    rows: Sequence[dict[str, Any]] | None = None,
) -> list[ExternalPair]:
    """Same-instruction, same-model mixed pairs from OpenHands sampled rollouts."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    source = rows if rows is not None else iter_openhands_rows(dataset, split)
    for row in source:
        if model is not None and row["model"] != model:
            continue
        grouped.setdefault((row["instance_id"], row["model"]), []).append(row)

    pairs: list[ExternalPair] = []
    for (instance, name), rollouts in sorted(grouped.items()):
        wins = [r for r in rollouts if r["resolved"]]
        losses = [r for r in rollouts if not r["resolved"]]
        if not wins or not losses:
            continue
        candidates: list[tuple[list[Step], list[Step], str, str]] = []
        for w in wins:
            for loss in losses:
                good = strip_terminal(to_steps(w["messages"], shell_verbs=shell_verbs))
                bad = strip_terminal(to_steps(loss["messages"], shell_verbs=shell_verbs))
                if good and bad:
                    candidates.append((good, bad, w["run_id"], loss["run_id"]))
        if not candidates:
            continue
        # Run ids break ties so reloads pick the same pair the key sheet named.
        good, bad, good_id, bad_id = max(
            candidates,
            key=lambda gb: (
                min(len(gb[0]), len(gb[1])),
                max(len(gb[0]), len(gb[1])),
                gb[2],
                gb[3],
            ),
        )
        pair = ExternalPair(
            instance, name, tuple(good), tuple(bad), good_id, bad_id
        )
        if pair.length_ratio <= max_ratio:
            pairs.append(pair)
    return pairs


def select_openhands_pairs(
    pairs: Sequence[ExternalPair],
    n: int = 30,
    seed: int = SELECT_SEED,
) -> list[ExternalPair]:
    """Deterministic subset for labeling — seeded shuffle, then take n."""
    if n <= 0 or n >= len(pairs):
        return list(pairs)
    order = list(pairs)
    rng = random.Random(seed)
    rng.shuffle(order)
    return order[:n]


def iter_shards(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.parquet"))


def _measure(pairs: Sequence[ExternalPair]) -> dict[str, Any]:
    import statistics
    from dataclasses import replace as _replace

    from tracewake.align import (
        DEFAULT_CONFIG,
        LexicalEmbedder,
        align,
        divergence_step,
        first_target_difference,
    )

    embed = LexicalEmbedder()
    linear = _replace(DEFAULT_CONFIG, gap_extend=DEFAULT_CONFIG.gap_open)
    lengths: list[int] = []
    last_step = first_diff_one = same_as_first_diff = gap_shape_matters = 0
    for pair in pairs:
        good, bad = list(pair.good), list(pair.bad)
        _, aligned, _ = align(good, bad, embed=embed)
        _, linear_aligned, _ = align(good, bad, embed=embed, config=linear)
        index = divergence_step(aligned, good, bad) or len(bad)
        linear_index = divergence_step(linear_aligned, good, bad) or len(bad)
        baseline = first_target_difference(good, bad)
        lengths.append(len(bad))
        last_step += index == len(bad)
        first_diff_one += baseline == 1
        same_as_first_diff += index == baseline
        gap_shape_matters += index != linear_index
    n = len(pairs) or 1
    return {
        "pairs": len(pairs),
        "median_failing_steps": statistics.median(lengths) if lengths else 0,
        "max_failing_steps": max(lengths, default=0),
        "aligner_on_last_step": last_step,
        "first_diff_is_step_one": first_diff_one,
        "aligner_equals_first_diff": same_as_first_diff,
        "contestable": len(pairs) - same_as_first_diff,
        "affine_changes_answer": gap_shape_matters,
        "n": n,
    }


def report_swesmith(shard: Path, model: str = "claude-3-5-sonnet-20241022") -> str:
    """Structural comparison for a SWE-smith shard. No accuracy — no labels."""
    lines = [
        f"external trajectories: {shard.name}",
        f"model {model}; one good/bad pair per mixed-outcome task, same scaffold",
        "",
    ]
    for verbs, label in ((True, "shell verb (find/grep/cd)"), (False, "raw tool name (bash)")):
        pairs = load_pairs(shard, model=model, shell_verbs=verbs)
        m = _measure(pairs)
        n = m["n"]
        lines += [
            f"step unit = {label}",
            f"  pairs                        {m['pairs']}",
            f"  failing steps                median {m['median_failing_steps']:.0f}, "
            f"max {m['max_failing_steps']}",
            f"  aligner lands on last step   {m['aligner_on_last_step']}/{n} "
            f"({m['aligner_on_last_step'] / n:.0%})",
            f"  first-difference is step 1   {m['first_diff_is_step_one']}/{n} "
            f"({m['first_diff_is_step_one'] / n:.0%})",
            f"  aligner == first-difference  {m['aligner_equals_first_diff']}/{n} "
            f"(contestable {m['contestable']}/{n})",
            f"  affine gaps change the answer {m['affine_changes_answer']}/{n} "
            f"({m['affine_changes_answer'] / n:.0%})",
            "",
        ]
    lines.append(
        "No labels exist for these pairs, so none of this is an accuracy claim. "
        "The step unit is a modelling choice and both readings are reported. "
        "SWE-smith same-instruction pairing is empty; prefer `bench external openhands`."
    )
    return "\n".join(lines)


def report_openhands(
    *,
    model: str | None = "gpt-4o-2024-08-06",
    dataset: str = OPENHANDS_DATASET,
) -> str:
    lines = [
        f"external trajectories: {dataset}",
        f"model {model or 'any'}; same OpenHands scaffold; finish stripped",
        "",
    ]
    for verbs, label in (
        (True, "shell verb (find/grep/cd)"),
        (False, "raw tool name (execute_bash)"),
    ):
        pairs = load_openhands_pairs(dataset=dataset, model=model, shell_verbs=verbs)
        m = _measure(pairs)
        n = m["n"]
        lines += [
            f"step unit = {label}",
            f"  pairs                        {m['pairs']}",
            f"  failing steps                median {m['median_failing_steps']:.0f}, "
            f"max {m['max_failing_steps']}",
            f"  aligner lands on last step   {m['aligner_on_last_step']}/{n} "
            f"({m['aligner_on_last_step'] / n:.0%})",
            f"  first-difference is step 1   {m['first_diff_is_step_one']}/{n} "
            f"({m['first_diff_is_step_one'] / n:.0%})",
            f"  aligner == first-difference  {m['aligner_equals_first_diff']}/{n} "
            f"(contestable {m['contestable']}/{n})",
            f"  affine gaps change the answer {m['affine_changes_answer']}/{n} "
            f"({m['affine_changes_answer'] / n:.0%})",
            "",
        ]
    lines.append(
        "Structural only until hand labels exist under "
        f"{EXTERNAL_LABEL_ROOT}. Scout inventory: {SCOUT_PATH}."
    )
    return "\n".join(lines)


def report_scout() -> str:
    if not SCOUT_PATH.is_file():
        return f"no scout inventory at {SCOUT_PATH}"
    data = json.loads(SCOUT_PATH.read_text(encoding="utf-8"))
    lines = [
        "external trajectory scout",
        f"decision: {data.get('decision')}",
        f"adapter follow-on: {data.get('adapter_follow_on')}",
        "",
    ]
    for src in data.get("sources", []):
        mark = "*" if src.get("chosen") else "-"
        lines.append(f"{mark} {src['id']}")
        if "same_model_pairs_ratio_le_4" in src:
            lines.append(
                f"    same-model pairs (ratio≤4): {src['same_model_pairs_ratio_le_4']}"
            )
        if "same_instruction_pairs" in src:
            lines.append(f"    same-instruction pairs: {src['same_instruction_pairs']}")
        lines.append(f"    {src.get('why', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Blind labeling export + score (external sheet; synthetic predictions untouched)
# ---------------------------------------------------------------------------


def _steps_as_label_rows(steps: Sequence[Step]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, step in enumerate(steps, start=1):
        out.append(
            {
                "step": i,
                "name": step.name,
                "target": step.target or "",
                "args": json.dumps(step.args, sort_keys=True),
                "status": "ok",
                "reason": " ".join(step.reasoning.split()) if step.reasoning else "",
            }
        )
    return out


def _paths_in(text: str) -> list[str]:
    found: list[str] = []
    for token in text.replace("'", " ").replace('"', " ").split():
        cleaned = token.strip(".,;:()[]{}")
        if ".py" not in cleaned:
            continue
        end = cleaned.find(".py") + 3
        cleaned = cleaned[:end]
        if cleaned not in found:
            found.append(cleaned)
    return found


def _anonymize(good: list[dict], bad: list[dict]) -> tuple[list[dict], list[dict]]:
    paths: list[str] = []
    for step in (*good, *bad):
        for field in ("target", "args", "reason"):
            for path in _paths_in(step[field]):
                if path not in paths:
                    paths.append(path)
    paths.sort(key=len, reverse=True)
    if len(paths) > 26:
        mapping = {path: f"file_{i + 1}" for i, path in enumerate(paths)}
    else:
        mapping = {path: f"file_{chr(ord('a') + i)}" for i, path in enumerate(paths)}

    def rewrite(step: dict) -> dict:
        out = dict(step)
        for field in ("target", "args", "reason"):
            text = out[field]
            for path, alias in mapping.items():
                text = text.replace(path, alias)
            out[field] = text
        if len(out["args"]) > ARG_CHARS:
            out["args"] = out["args"][: ARG_CHARS - 1] + "…"
        if len(out["reason"]) > REASON_CHARS:
            out["reason"] = out["reason"][: REASON_CHARS - 1] + "…"
        return out

    return ([rewrite(s) for s in good], [rewrite(s) for s in bad])


def _render_side(label: str, trajectory: list[dict]) -> str:
    lines = [f"## {label}  ({len(trajectory)} steps)", ""]
    for step in trajectory:
        head = f"{step['step']:>2}. {step['name']}"
        if step["target"]:
            head += f"  → {step['target']}"
        lines.append(head)
        lines.append(f"    args: {step['args']}")
        if step["reason"]:
            wrapped = textwrap.fill(
                step["reason"],
                width=88,
                initial_indent="    reason: ",
                subsequent_indent="            ",
            )
            lines.append(wrapped)
        lines.append("")
    return "\n".join(lines)


def _render_packet(packet_id: str, good: list[dict], bad: list[dict]) -> str:
    return "\n".join(
        [
            f"# Packet {packet_id}",
            "",
            "Label the 1-based FAILURE step where the runs stopped agreeing.",
            "SUCCESS is the resolved OpenHands rollout; FAILURE is unresolved.",
            "Ignore a shared terminal `finish` — it was already stripped.",
            "",
            _render_side("SUCCESS", good),
            _render_side("FAILURE", bad),
        ]
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def export_openhands_packets(
    *,
    n: int = 30,
    seed: int = SELECT_SEED,
    dest: Path = EXTERNAL_LABEL_ROOT,
    model: str | None = "gpt-4o-2024-08-06",
    extend: bool = False,
    pairs: Sequence[ExternalPair] | None = None,
) -> Path:
    """Blind packets for external transfer labeling. Key must stay closed.

    `extend` grows a sheet that already has labels in it. The seeded selection
    shuffles once and takes a prefix, so a larger `n` is a superset of a
    smaller one; already-keyed pairs therefore keep the packet id they were
    labeled under, and only the pairs the sheet has never seen get new ids.
    Without it the whole sheet is rewritten and every existing label silently
    starts pointing at a different pair.
    """
    loaded = pairs if pairs is not None else load_openhands_pairs(model=model)
    selected = select_openhands_pairs(loaded, n=n, seed=seed)
    if not selected:
        raise RuntimeError(
            "no OpenHands pairs to export. Install `datasets`, ensure network "
            "access to Hugging Face, and re-run."
        )
    packets_dir = dest / "packets"
    packets_dir.mkdir(parents=True, exist_ok=True)
    key_path = dest / "key.jsonl"
    sheet_path = dest / "labels.jsonl"

    key_rows = _read_jsonl(key_path) if extend else []
    sheet_rows = _read_jsonl(sheet_path) if extend else []
    if len(key_rows) != len(sheet_rows):
        raise RuntimeError(
            f"{key_path.name} has {len(key_rows)} packets but {sheet_path.name} has "
            f"{len(sheet_rows)}. Refusing to extend a sheet whose key and labels "
            f"disagree — re-export without --extend to rebuild both."
        )
    if not extend:
        for old in packets_dir.glob("*.md"):
            old.unlink()

    # Keyed by instance, which is what a sheet entry actually is — the loader
    # yields one pair per instance. Run ids are the sampling configuration,
    # shared by thousands of rollouts, and which rollout wins a length tie has
    # changed before; either makes an already-labeled instance come back under
    # a second packet id.
    keyed = {row["instance_id"] for row in key_rows}
    fresh = [p for p in selected if p.instance_id not in keyed]
    order = list(range(len(fresh)))
    random.Random(seed + 41 + len(key_rows)).shuffle(order)
    start = len(key_rows)
    for offset, source_i in enumerate(order):
        pair = fresh[source_i]
        good, bad = _anonymize(
            _steps_as_label_rows(pair.good), _steps_as_label_rows(pair.bad)
        )
        packet_id = f"E{start + offset + 1:02d}"
        (packets_dir / f"{packet_id}.md").write_text(
            _render_packet(packet_id, good, bad), encoding="utf-8"
        )
        key_rows.append(
            {
                "packet_id": packet_id,
                "instance_id": pair.instance_id,
                "model": pair.model,
                "good_run_id": pair.good_run_id,
                "bad_run_id": pair.bad_run_id,
                "good_steps": len(pair.good),
                "bad_steps": len(pair.bad),
            }
        )
        sheet_rows.append({"packet_id": packet_id, "label": None, "note": ""})
    key_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in key_rows), encoding="utf-8"
    )
    sheet_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in sheet_rows), encoding="utf-8"
    )
    (dest / "README.txt").write_text(
        "\n".join(
            [
                "External transfer labels (OpenHands-Sampled-Trajectories).",
                "  packets/     blinded markdown; do not open key.jsonl while labeling.",
                "  key.jsonl    packet_id → instance/run ids.",
                "  labels.jsonl fill `label` with the 1-based FAILURE step.",
                f"{len(key_rows)} pairs. Seed {seed}.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return dest


def score_openhands_labels(
    *,
    labels_path: Path = EXTERNAL_LABEL_ROOT / "labels.jsonl",
    key_path: Path = EXTERNAL_LABEL_ROOT / "key.jsonl",
    model: str | None = "gpt-4o-2024-08-06",
    out: Path = EXTERNAL_PRED,
    rows: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Score frozen aligner against filled external labels. Separate sheet of record."""
    from tracewake.align import (
        LexicalEmbedder,
        align,
        divergence_step,
        first_target_difference,
        last_common_prefix,
    )

    if not labels_path.is_file() or not key_path.is_file():
        raise FileNotFoundError(
            f"need {labels_path} and {key_path}. Run `python -m bench external export` first."
        )
    labels = {
        json.loads(line)["packet_id"]: json.loads(line)
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    keys = [
        json.loads(line)
        for line in key_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    filled = [k for k in keys if labels.get(k["packet_id"], {}).get("label") is not None]
    if not filled:
        return (
            f"no filled labels in {labels_path}. Label packets under "
            f"{EXTERNAL_LABEL_ROOT / 'packets'}, then re-run score."
        )

    # Score the exact runs the key sheet named — not a re-derived "best" pair,
    # which can flip when several candidates share the same length key.
    #
    # Keyed by instance as well as run id: `run_id` names the sampling
    # configuration ("...-t0-run_1"), and all 5.8k rollouts share eight of
    # them. Keyed on run id alone the table holds eight rows, and every packet
    # silently scores against whichever instance landed there last.
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows if rows is not None else iter_openhands_rows():
        if model is not None and row["model"] != model:
            continue
        by_id[(row["instance_id"], row["run_id"])] = row

    embed = LexicalEmbedder()
    rows_out: list[dict[str, Any]] = []
    for key in filled:
        lab = int(labels[key["packet_id"]]["label"])
        win = by_id.get((key["instance_id"], key["good_run_id"]))
        loss = by_id.get((key["instance_id"], key["bad_run_id"]))
        if win is None or loss is None:
            raise KeyError(
                f"packet {key['packet_id']} names runs not in {OPENHANDS_DATASET}"
            )
        good = strip_terminal(to_steps(win["messages"], shell_verbs=True))
        bad = strip_terminal(to_steps(loss["messages"], shell_verbs=True))
        _, aligned, _ = align(good, bad, embed=embed)
        pred = divergence_step(aligned, good, bad)
        rows_out.append(
            {
                "packet_id": key["packet_id"],
                "instance_id": key["instance_id"],
                "label": lab,
                "aligner": pred if pred is not None else len(bad),
                "abstained": pred is None,
                "baseline_a": first_target_difference(good, bad),
                "baseline_b": last_common_prefix(good, bad),
                "failure_steps": len(bad),
            }
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "source": OPENHANDS_DATASET,
                        "n": len(rows_out),
                        "labels": corpus_metadata_path(labels_path),
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        for row in rows_out:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    lines = [
        "external alignment evaluation (OpenHands-Sampled)",
        f"n={len(rows_out)}  labels={labels_path.name}  sheet={out}",
    ]
    # Split at the median failing length. A constant answer does well when
    # labels cluster near the start, so a whole-set number says little on its
    # own; the two halves are where the aligner and a constant come apart.
    cut = statistics.median(r["failure_steps"] for r in rows_out)
    lines += _external_metrics(rows_out, "all pairs")
    lines += _external_metrics(
        [r for r in rows_out if r["failure_steps"] <= cut], f"failure ≤{cut:.0f} steps"
    )
    lines += _external_metrics(
        [r for r in rows_out if r["failure_steps"] > cut], f"failure >{cut:.0f} steps"
    )
    return "\n".join(lines)


def _external_metrics(group: Sequence[dict[str, Any]], name: str) -> list[str]:
    from .aligneval import mcnemar, median_abs_error, oracle_constant, within_tol

    if not group:
        return ["", f"{name}: (empty)"]
    labels = [r["label"] for r in group]
    n = len(group)
    lines = ["", f"{name}: n={n}"]
    hits_by: dict[str, list[bool]] = {}
    for method in ("aligner", "baseline_a", "baseline_b"):
        values = [r[method] if r[method] is not None else -(10**9) for r in group]
        hits = [within_tol(v, y) for v, y in zip(values, labels, strict=True)]
        hits_by[method] = hits
        lines.append(
            f"  {method:<12} within±2 {sum(hits)}/{n} ({sum(hits) / n:.1%})  "
            f"median |err| {median_abs_error(values, labels):.1f}"
        )
    k = oracle_constant(labels)
    k_hits = sum(1 for y in labels if within_tol(k, y))
    lines.append(
        f"  {'oracle_k':<12} within±2 {k_hits}/{n} ({k_hits / n:.1%})  "
        f"(best constant k={k}; ceiling diagnostic, fit on these labels)"
    )
    for method in ("baseline_a", "baseline_b"):
        b_only, a_only, p = mcnemar(hits_by["aligner"], hits_by[method])
        p_txt = f"{p:.3f}" if p is not None else "n/a"
        lines.append(
            f"  McNemar vs {method}: aligner-only {a_only}, "
            f"baseline-only {b_only}, p={p_txt}"
        )
    abstained = sum(1 for r in group if r["abstained"])
    lines.append(f"  aligner abstained (scored as last step): {abstained}/{n}")
    return lines


# Back-compat name used by older CLI wiring.
report = report_swesmith
