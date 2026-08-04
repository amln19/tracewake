"""Alignment against trajectories this project did not record.

The aligner consumes step sequences and nothing else — no cassettes, no
provenance, no replay — so trajectories somebody else published can be read
directly. What this checks is whether the corpus's shape, not just its numbers,
generalises: those runs are a local 7B on small repositories with a median of
six actions, and the design decisions the ablations could not justify (affine
gaps, embeddings) are the ones that only pay off on longer trajectories.

Source: SWE-agent rollouts published as `SWE-bench/SWE-smith-trajectories`.
Several rollouts of one model on one task, graded pass/fail, so a mixed-outcome
task yields a good/bad pair from a single scaffold. Pairing across models would
confound "where did these part" with "these agents do not share a vocabulary".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locus.align import Step

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


@dataclass(frozen=True)
class ExternalPair:
    instance_id: str
    model: str
    good: tuple[Step, ...]
    bad: tuple[Step, ...]

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


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def to_steps(
    messages: Sequence[dict[str, Any]], *, shell_verbs: bool = True
) -> list[Step]:
    """Assistant turns that took an action, as the alignment sequence.

    `shell_verbs` splits `bash` into the command's first word. It changes what
    counts as the same action, so both settings are reported rather than one
    being assumed.
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
        for name, args in calls:
            if name == _SHELL:
                verb, args = _shell_step(str(args.get("command", "")))
                name = verb if shell_verbs else _SHELL
            elif name == _EDITOR:
                if str(args.get("command", "")) in _EDIT_COMMANDS and args.get("path"):
                    # Content hashes are not recoverable here, so the digest slot
                    # carries the edit kind. Jaccard still separates "touched a
                    # different file" from "touched the same one".
                    changed.add((str(args["path"]), str(args.get("command"))))
                name = f"{_EDITOR}.{args.get('command', 'view')}"
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
            )
        )
    return steps


def strip_terminal(steps: Sequence[Step]) -> list[Step]:
    """Drop the trailing `submit`, which every rollout of this harness ends on.

    The divergence rule walks backward to the last column that agrees. A
    terminal action both runs always take is always that column, so the rule
    reports "they re-align through the end" for every pair and the index
    collapses onto the last step. The corpus never showed this because it
    recorded `submit` only for runs that actually submitted, and most failing
    runs there ran out of budget instead.
    """
    out = list(steps)
    while out and out[-1].name == "submit":
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
    grouped: dict[tuple[str, str], list[tuple[bool, str]]] = {}
    for instance, resolved, name, messages in zip(
        columns["instance_id"], columns["resolved"], columns["model"], columns["messages"],
        strict=True,
    ):
        if model is not None and name != model:
            continue
        grouped.setdefault((instance, name), []).append((bool(resolved), messages))

    pairs: list[ExternalPair] = []
    for (instance, name), rollouts in sorted(grouped.items()):
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


def iter_shards(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.parquet"))


def _measure(pairs: Sequence[ExternalPair]) -> dict[str, Any]:
    import statistics
    from dataclasses import replace as _replace

    from locus.align import (
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


def report(shard: Path, model: str = "claude-3-5-sonnet-20241022") -> str:
    """Structural comparison against the corpus. No accuracy — there are no labels."""
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
        "The step unit is a modelling choice and both readings are reported."
    )
    return "\n".join(lines)
