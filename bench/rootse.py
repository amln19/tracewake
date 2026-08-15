"""RootSE: externally labelled failure trajectories, paired with a passing run.

RootSE (Wang, Xie & Huo, *TrajAudit*) annotates 102 failed agent runs with the
earliest decisive error step — "the earliest step which, if corrected, would
change the outcome from failure to success". That is independently the target
`.project-notes` defines for Tracewake, arrived at by other people, so it is
worth far more than another self-labelled set: nobody here chose these labels.

The value and the risks both need stating plainly.

Value: the labels are free and were made by annotators who never saw Tracewake;
the failing traces are long (median 51 steps against 18 in the OpenHands set),
which is exactly where `align-v1` collapses; and four different agent
scaffolds are represented, so it tests whether a profile travels.

Risks: the passing reference for an instance generally comes from a *different
backbone model* than the failure, so more of the difference between the two
runs is model idiom than in a same-model pair. And the label distribution sits
earlier in the trace (median 0.44 of the way through, against 0.61 here), so a
constant fitted on Tracewake's development data does not transfer and must not
be quietly refitted on these labels.

Data is not vendored — it is a 200MB third-party repository. Clone it and point
`ROOTSE_ROOT` at the checkout:

    git clone https://github.com/LogAnalysisTech/TrajAudit corpus/external/TrajAudit
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tracewake.align import Step

from .repos import CORPUS_ROOT

ROOTSE_ROOT = Path(
    os.environ.get("ROOTSE_ROOT", CORPUS_ROOT / "external" / "TrajAudit")
)

EDITOR = "str_replace_editor"
EDIT_COMMANDS = frozenset({"str_replace", "create", "insert"})
TERMINAL = frozenset({"submit", "finish"})

# A bash step writes when it redirects, edits in place, or applies a patch.
# Deliberately conservative: a false positive invents a commitment and moves the
# reported divergence earlier, which is worse than missing one.
#
# The `>` must not be part of `=>`, `->`, `>=` or a numbered stream (`2>&1`).
# Arrow functions and comparisons inside `node -e` / `python -c` payloads are
# otherwise read as redirects.
_REDIRECT = re.compile(r"(?<![=\-!<>0-9])>>?\s*([^\s|&;<>]+)")
_SED_INPLACE = re.compile(r"\bsed\b[^|;]*?\s-i\b[^|;]*?\s([^\s|&;]+)\s*$")
_TEE = re.compile(r"\btee\b\s+(?:-a\s+)?([^\s|&;]+)")
_EDITOR_PY = re.compile(r"editor\.py\s+(replace|insert|create|write)\s+(\S+)")
_PATCH = re.compile(r"\b(?:applypatch|git\s+apply|patch\s+-)")
# `cat <<'EOF' > file` is one command followed by the file's contents. Scanning
# the body finds every `>` in the payload — diff markers, arrow functions, HTML.
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
# A redirect target is a filename. `v`, `1:` and `")` are parser debris.
_PATHLIKE = re.compile(r"^[\w./~$-][\w./~$@+-]*$")


def _command_head(command: str) -> str:
    """The command itself, without any heredoc body it carries."""
    match = _HEREDOC.search(command)
    if match is None:
        return command
    end = command.find("\n")
    return command if end == -1 else command[:end]


def _bash_writes(command: str) -> set[str]:
    head = _command_head(command)
    out: set[str] = set()
    for pattern in (_SED_INPLACE, _TEE):
        out.update(m.group(1) for m in pattern.finditer(head))
    out.update(m.group(2) for m in _EDITOR_PY.finditer(head))
    for match in _REDIRECT.finditer(head):
        target = match.group(1)
        # `2>&1` and `>/dev/null` are not edits to the project.
        if target.startswith(("&", "/dev/")):
            continue
        out.add(target)
    if _PATCH.search(head):
        out.add("<patch>")
    return {p for p in out if p and _PATHLIKE.match(p)}


# The environment reporting a failed edit means no write happened. Counting the
# attempt would place a commitment where the file never changed.
_EDIT_FAILED = re.compile(
    r"No replacement was performed"
    r"|did not appear verbatim"
    r"|^ *ERROR:"
    r"|Invalid `?(?:path|view_range|file_text)"
    r"|No such file or directory"
    r"|<returncode>[1-9]\d*</returncode>",
    re.IGNORECASE | re.MULTILINE,
)


def wrote_nothing(observation: str) -> bool:
    return bool(_EDIT_FAILED.search(observation or ""))


def _bash_step(command: str) -> tuple[str, dict, str, set[str]]:
    tokens = command.split()
    verb = tokens[0] if tokens else "bash"
    path = next(
        (t for t in tokens[1:] if "/" in t and not t.startswith("-")), ""
    )
    return verb, {"command": command}, path.rstrip(";|&"), _bash_writes(command)


def _editor_step(command: str, path: str, payload: dict) -> tuple[str, dict, str, set[str]]:
    writes = {path} if command in EDIT_COMMANDS and path else set()
    return f"{EDITOR}.{command}", payload, path, writes


def decode_action(action) -> tuple[str, dict, str, set[str]] | None:
    """One RootSE action -> (name, args, target, written paths).

    Four scaffolds, three encodings: OpenHands emits a structured dict,
    SWE-agent a shell string whose editor form is positional
    (`str_replace_editor <command> <path>`), and AutoCodeRover a list of
    function calls.
    """
    if isinstance(action, dict):
        tool = str(action.get("tool") or "")
        payload = dict(action.get("input") or {})
        if tool.endswith(EDITOR):
            return _editor_step(
                str(payload.get("command", "view")), str(payload.get("path", "")), payload
            )
        if tool in ("execute_bash", "bash"):
            return _bash_step(str(payload.get("command", "")))
        if tool in TERMINAL:
            return (tool, payload, "", set())
        return (tool or "unknown", payload, str(payload.get("path", "")), set())

    if isinstance(action, list):
        # AutoCodeRover: search_* calls plus a terminal `write_patch`. The patch
        # itself carries no path, so it cannot anchor a commitment.
        names, args, writes = [], {}, set()
        for call in action:
            if not isinstance(call, dict):
                continue
            name = str(call.get("func_name") or "")
            names.append(name)
            args.update(call.get("arguments") or {})
            if name == "write_patch":
                writes.add("<patch>")
        if not names:
            return None
        return (names[0], args, str(args.get("file_name", "")), writes)

    text = (action or "").strip() if isinstance(action, str) else ""
    if not text:
        return None
    tokens = text.split()
    if tokens[0].strip("'\"") == EDITOR:
        command = tokens[1].strip("'\"") if len(tokens) > 1 else "view"
        path = tokens[2] if len(tokens) > 2 else ""
        return _editor_step(command, path, {"command": command, "path": path})
    if tokens[0] in TERMINAL:
        return (tokens[0], {}, "", set())
    return _bash_step(text)


def to_steps(trajectory, *, strip_terminal: bool = False) -> list[Step]:
    steps: list[Step] = []
    for raw in trajectory:
        decoded = decode_action(raw.get("action"))
        if decoded is None:
            # A step with no action is reasoning only. Keeping it preserves the
            # index base the label counts in, so it must not be dropped.
            decoded = ("(no-op)", {}, "", set())
        name, args, target, writes = decoded
        if writes and wrote_nothing(str(raw.get("observation") or "")):
            # The environment rejected the edit, so nothing was committed to.
            writes = set()
        reasoning = str(raw.get("thought") or raw.get("response") or "")
        steps.append(
            Step(
                name=name,
                args=args,
                target=target,
                reasoning=" ".join(reasoning.split()),
                writes=frozenset(writes),
                observation=str(raw.get("observation") or ""),
            )
        )
    if strip_terminal:
        while steps and steps[-1].name in TERMINAL:
            steps.pop()
    return steps


@dataclass(frozen=True)
class RootSEPair:
    instance_id: str
    agent: str
    model: str
    # 1-based on the failing side, converted from RootSE's 0-based failure_id.
    label: int
    good: list[Step]
    bad: list[Step]
    reference: str


def _reference_dirs(root: Path, instance_id: str) -> list[Path]:
    base = root / "baseline" / "FAMAS" / "reference_trajs"
    return [
        *base.glob(f"*/*/instance_{instance_id}/success"),
        *base.glob(f"*/*/{instance_id}/success"),
    ]


@dataclass(frozen=True)
class RootSEFailure:
    """One annotated failing run, with no reference. What a single-trace rule needs."""

    instance_id: str
    agent: str
    model: str
    label: int
    bad: list[Step]


def load_failures(root: Path = ROOTSE_ROOT) -> list[RootSEFailure]:
    """Every annotated failing run, whether or not a passing reference exists.

    `load_pairs` keeps only the 58 instances that ship a passing run, because a
    reference-based rule cannot attempt the rest. A single-trace rule can, and
    the other 44 carry the same external annotation. They are the scarcest thing
    this project has -- labels written by other people -- and they had been
    going unused because the pair loader was the only way in.
    """
    if not (root / "RootSE").is_dir():
        raise FileNotFoundError(f"no RootSE checkout at {root}")
    out: list[RootSEFailure] = []
    for path in sorted((root / "RootSE").glob("*/*/*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        bad = to_steps(row["original_traj"])
        marker = str(row.get("failure_id", "")).strip()
        if not bad or not marker:
            continue
        out.append(
            RootSEFailure(
                instance_id=row["instance_id"],
                agent=str(row.get("agent") or ""),
                model=str(row.get("model") or ""),
                label=int(marker) + 1,
                bad=bad,
            )
        )
    return out


def load_pairs(root: Path = ROOTSE_ROOT) -> list[RootSEPair]:
    """Every RootSE instance that has at least one passing reference run.

    Reference choice is method-independent and mirrors the corpus rule:
    maximise the shorter trajectory, then the longer, then the file name.
    """
    if not (root / "RootSE").is_dir():
        raise FileNotFoundError(
            f"no RootSE checkout at {root}. Clone it with\n"
            f"  git clone https://github.com/LogAnalysisTech/TrajAudit "
            f"{CORPUS_ROOT / 'external' / 'TrajAudit'}\n"
            f"or set ROOTSE_ROOT to an existing checkout."
        )
    out: list[RootSEPair] = []
    for path in sorted((root / "RootSE").glob("*/*/*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        bad = to_steps(row["original_traj"])
        candidates = []
        for directory in _reference_dirs(root, row["instance_id"]):
            for ref in sorted(directory.glob("*.json")):
                data = json.loads(ref.read_text(encoding="utf-8"))
                if not data.get("passed"):
                    continue
                good = to_steps(data.get("steps") or [])
                if good:
                    candidates.append((min(len(good), len(bad)), len(good), ref.name, good))
        if not candidates:
            continue
        _, _, name, good = max(candidates)
        out.append(
            RootSEPair(
                instance_id=row["instance_id"],
                agent=str(row.get("agent") or ""),
                model=str(row.get("model") or ""),
                label=int(str(row["failure_id"]).strip()) + 1,
                good=good,
                bad=bad,
                reference=name,
            )
        )
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

# Frozen on the OpenHands development half (corpus/alignment/dev-fitted.json).
# Deliberately not refitted on RootSE: refitting would convert an externally
# labelled transfer set into development data.
DEV_CONSTANT_K = 10
DEV_PROPORTIONAL_C = 0.66

SHEET = CORPUS_ROOT / "alignment" / "predictions-rootse.jsonl"


def _predict(good, bad):
    from tracewake.align import (
        LexicalEmbedder,
        align,
        divergence_step,
        first_target_difference,
        last_common_prefix,
    )
    from tracewake.diverge import earliest_bound, reliability

    embed = LexicalEmbedder()
    _, aligned, _ = align(good, bad, embed=embed)
    lexical = divergence_step(aligned, good, bad)

    n = len(bad)
    values = {
        "earliest_bound": earliest_bound(bad),
        "align-v1": lexical if lexical is not None else n,
        "first-difference": first_target_difference(good, bad),
        "last-common-prefix": last_common_prefix(good, bad),
        f"constant-{DEV_CONSTANT_K}": max(1, min(n, DEV_CONSTANT_K)),
        f"proportional-{DEV_PROPORTIONAL_C}": max(
            1, min(n, round(DEV_PROPORTIONAL_C * n))
        ),
    }
    return values, reliability(bad)


def evaluate(root: Path = ROOTSE_ROOT, out: Path = SHEET) -> str:
    import statistics

    from .aligneval import mcnemar, oracle_constant, within_tol

    pairs = load_pairs(root)
    rows = []
    for pair in pairs:
        values, klass = _predict(pair.good, pair.bad)
        rows.append(
            {
                "instance_id": pair.instance_id,
                "agent": pair.agent,
                "model": pair.model,
                "reference": pair.reference,
                "label": pair.label,
                "failure_steps": len(pair.bad),
                "reference_steps": len(pair.good),
                "reliability": klass,
                **values,
            }
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "source": "RootSE (github.com/LogAnalysisTech/TrajAudit)",
                        "n": len(rows),
                        "labels": "external — RootSE failure_id, 0-based, +1 here",
                        "constants": "frozen from the OpenHands development half",
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    names = [k for k in rows[0] if k not in
             ("instance_id", "agent", "model", "reference", "label",
              "failure_steps", "reference_steps", "reliability")]

    def block(group, title):
        if not group:
            return []
        labels = [r["label"] for r in group]
        lines = [f"\n{title}: n={len(group)}",
                 f"  {'method':<26}{'exact':>7}{'±1':>5}{'±2':>5}{'meanAE':>9}"]
        hits = {}
        for name in names:
            values = [r[name] for r in group]
            hit = [within_tol(v, y) for v, y in zip(values, labels, strict=True)]
            hits[name] = hit
            lines.append(
                f"  {name:<26}"
                f"{sum(1 for v, y in zip(values, labels, strict=True) if v == y):>7}"
                f"{sum(1 for v, y in zip(values, labels, strict=True) if abs(v - y) <= 1):>5}"
                f"{sum(hit):>5}"
                f"{statistics.mean(abs(v - y) for v, y in zip(values, labels, strict=True)):>9.2f}"
            )
        k = oracle_constant(labels)
        lines.append(
            f"  {'oracle-constant':<26}{'':>7}{'':>5}"
            f"{sum(1 for y in labels if within_tol(k, y)):>5}"
            f"{statistics.mean(abs(k - y) for y in labels):>9.2f}"
            f"   (k={k}; fitted on these labels — ceiling diagnostic)"
        )
        for name in names:
            if name == "earliest_bound":
                continue
            b_only, a_only, p = mcnemar(hits["earliest_bound"], hits[name])
            lines.append(
                f"  McNemar earliest_bound vs {name}: +{a_only} -{b_only} "
                f"n_disc={a_only + b_only} p={f'{p:.3f}' if p is not None else 'n/a'}"
            )
        return lines

    lines = [
        "RootSE transfer evaluation (external labels, never used for design)",
        f"source {root}  sheet {out.name}",
    ]
    lines += block(rows, "all pairs")
    lines += block([r for r in rows if r["failure_steps"] > 18], "long failures (>18 steps)")
    lines += block([r for r in rows if r["failure_steps"] <= 18], "short failures (<=18 steps)")
    for agent in sorted({r["agent"] for r in rows}):
        lines += block([r for r in rows if r["agent"] == agent], f"scaffold: {agent}")
    return "\n".join(lines)
