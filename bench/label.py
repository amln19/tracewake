"""Blind divergence labeling for the alignment evaluation set.

Pair selection and packet export are deterministic given the corpus and the seed.
The key that maps anonymous packet ids back to task/run ids is written beside the
packets and must not be consulted during a labeling pass.
"""

from __future__ import annotations

import json
import random
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from locus import ModelCallEvent, Store, ToolCallEvent

from .fidelity import GRANULARITIES, extract, pair_up, target_of
from .repos import CORPUS_ROOT
from .runner import LEDGER, STORE

LABEL_ROOT = CORPUS_ROOT / "labels"
LABELS_FILE = "labels.jsonl"
SELECT_SEED = 20260730
# Bumped once after a pre-anonymization export briefly exposed package paths on
# the page under the previous shuffle; a fresh shuffle keeps that glimpse from
# anchoring the corresponding packet id.
SHUFFLE_SEED = 20260730 + 41
REASON_CHARS = 280
ARG_CHARS = 160


@dataclass(frozen=True)
class SelectedPair:
    task_id: str
    good_run: str
    bad_run: str
    good_actions: int
    bad_actions: int
    good_resolve: bool
    bad_resolve: bool
    length_ratio: float
    n_candidates: int


def select_pairs(
    store: Path = STORE, ledger: Path = LEDGER, seed: int = SELECT_SEED
) -> list[SelectedPair]:
    """One coverage-mixed pair per task under 4:1, by a method-independent rule.

    Among a task's candidates, maximise the shorter trajectory, then the longer,
    then break ties with a seeded shuffle over run ids. Nothing about baseline
    scores, gaps, or subset membership enters the choice.
    """
    rows, extracted = extract(store, ledger)
    by_id = {r["run_id"]: r for r in rows}
    _, target_key = GRANULARITIES[1]
    candidates = [
        c
        for c in pair_up(rows, extracted, target_key)
        if c.mixed and c.length_ratio <= 4
    ]
    by_task: dict[str, list] = {}
    for c in candidates:
        by_task.setdefault(c.task_id, []).append(c)

    rng = random.Random(seed)
    selected: list[SelectedPair] = []
    for task_id, group in sorted(by_task.items()):

        def sort_key(c):
            return (min(c.length_a, c.length_b), max(c.length_a, c.length_b))

        best = max(sort_key(c) for c in group)
        tied = sorted((c for c in group if sort_key(c) == best), key=lambda c: (c.a, c.b))
        rng.shuffle(tied)
        pick = tied[0]
        left, right = by_id[pick.a], by_id[pick.b]
        if left["coverage"] and not right["coverage"]:
            good, bad = left, right
        elif right["coverage"] and not left["coverage"]:
            good, bad = right, left
        else:
            raise RuntimeError(f"{task_id} pair is not mixed on coverage")
        selected.append(
            SelectedPair(
                task_id=task_id,
                good_run=good["run_id"],
                bad_run=bad["run_id"],
                good_actions=len(extracted[good["run_id"]]),
                bad_actions=len(extracted[bad["run_id"]]),
                good_resolve=bool(good["resolve"]),
                bad_resolve=bool(bad["resolve"]),
                length_ratio=round(
                    max(pick.length_a, pick.length_b) / min(pick.length_a, pick.length_b), 2
                ),
                n_candidates=len(group),
            )
        )
    return selected


def _trajectory(store: Store, run_id: str, stop_reason: str) -> list[dict]:
    """Raw steps with full text. Truncation happens after anonymization."""
    events = store.events(run_id)
    models = {
        e.event.call_id: e.event
        for e in events
        if isinstance(e.event, ModelCallEvent)
    }
    out: list[dict] = []
    for stored in events:
        event = stored.event
        if not isinstance(event, ToolCallEvent):
            continue
        index = int(event.tool_call_id.split("-", 1)[0].removeprefix("step"))
        if index != len(out):
            raise ValueError(
                f"tool call {event.tool_call_id!r} at position {len(out)} names step {index}"
            )
        parent = models.get(event.parent_call_id)
        reason = ""
        if parent is not None and parent.response.text:
            reason = " ".join(parent.response.text.split())
        args = dict(event.args)
        out.append(
            {
                "step": index + 1,
                "name": event.name,
                "target": target_of(args),
                "args": json.dumps(args, sort_keys=True),
                "status": event.status,
                "reason": reason,
            }
        )
    if stop_reason == "submitted":
        out.append(
            {
                "step": len(out) + 1,
                "name": "submit",
                "target": "",
                "args": "{}",
                "status": "ok",
                "reason": "",
            }
        )
    return out


def _paths_in(text: str) -> list[str]:
    found: list[str] = []
    for token in text.replace("'", " ").replace('"', " ").split():
        cleaned = token.strip(".,;:()[]{}")
        if ".py" not in cleaned:
            continue
        # Trim anything past .py (punctuation, JSON commas).
        end = cleaned.find(".py") + 3
        cleaned = cleaned[:end]
        # Slash paths and bare filenames both leak the task's package identity
        # (`bidict.py`, `test_schema.py`). Rewrite both.
        if cleaned not in found:
            found.append(cleaned)
    return found


def _anonymize(good: list[dict], bad: list[dict]) -> tuple[list[dict], list[dict]]:
    """Replace every path-like string with a packet-local token.

    Repo and package names sit inside ordinary paths (`parse/__init__.py`,
    `bidict/_bidict.py`). Leaving them on the page hands the annotator the task
    id's first component, which is exactly what stripping task ids was meant to
    hide. Paths are rewritten before any truncation so a cut-off path cannot
    leak the package name while escaping the matcher.
    """
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
        if step["status"] != "ok":
            head += f"  [{step['status']}]"
        lines.append(head)
        lines.append(f"    args: {step['args']}")
        if step["reason"]:
            wrapped = textwrap.fill(
                step["reason"], width=88, initial_indent="    reason: ", subsequent_indent="            "
            )
            lines.append(wrapped)
        lines.append("")
    return "\n".join(lines)


def _render_packet(packet_id: str, good: list[dict], bad: list[dict]) -> str:
    # Sides are labeled SUCCESS / FAILURE by coverage outcome only. No task id,
    # repo name, operator, run id, or computed gap appears on the page.
    return "\n".join(
        [
            f"# Packet {packet_id}",
            "",
            "Operational definition: the earliest step after which no subsequent",
            "step could plausibly recover the run. Index steps on the FAILURE side",
            "(the run that produced no applicable patch). If they never shared a",
            "recoverable path, the first FAILURE step. If FAILURE was doomed only",
            "at the end, the last step. Write one integer.",
            "",
            "Label: ________",
            "",
            _render_side("SUCCESS (produced an applicable patch)", good),
            _render_side("FAILURE (no applicable patch)", bad),
        ]
    )


def export_packets(
    dest: Path = LABEL_ROOT,
    store: Path = STORE,
    ledger: Path = LEDGER,
    select_seed: int = SELECT_SEED,
    shuffle_seed: int = SHUFFLE_SEED,
) -> Path:
    from .fidelity import ledger_rows

    selected = select_pairs(store, ledger, select_seed)
    rows = {r["run_id"]: r for r in ledger_rows(ledger)}
    db = Store(store)
    try:
        packets = []
        for pair in selected:
            good = _trajectory(db, pair.good_run, rows[pair.good_run]["stop_reason"])
            bad = _trajectory(db, pair.bad_run, rows[pair.bad_run]["stop_reason"])
            packets.append((pair, good, bad))
    finally:
        db.close()

    order = list(range(len(packets)))
    random.Random(shuffle_seed).shuffle(order)

    dest.mkdir(parents=True, exist_ok=True)
    packets_dir = dest / "packets"
    if packets_dir.exists():
        for old in packets_dir.glob("*.md"):
            old.unlink()
    else:
        packets_dir.mkdir()

    key_rows = []
    for display_i, source_i in enumerate(order, start=1):
        pair, good, bad = packets[source_i]
        good, bad = _anonymize(good, bad)
        packet_id = f"P{display_i:02d}"
        (packets_dir / f"{packet_id}.md").write_text(
            _render_packet(packet_id, good, bad), encoding="utf-8"
        )
        key_rows.append(
            {
                "packet_id": packet_id,
                "task_id": pair.task_id,
                "good_run": pair.good_run,
                "bad_run": pair.bad_run,
                "good_actions": pair.good_actions,
                "bad_actions": pair.bad_actions,
                "good_resolve": pair.good_resolve,
                "bad_resolve": pair.bad_resolve,
                "length_ratio": pair.length_ratio,
                "n_candidates": pair.n_candidates,
                "failure_steps": len(bad),
            }
        )

    key_path = dest / "key.jsonl"
    with key_path.open("w", encoding="utf-8") as fh:
        for row in key_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    sheet = dest / LABELS_FILE
    if not sheet.exists():
        with sheet.open("w", encoding="utf-8") as fh:
            for row in key_rows:
                fh.write(
                    json.dumps(
                        {"packet_id": row["packet_id"], "label": None, "note": ""},
                        sort_keys=True,
                    )
                    + "\n"
                )

    readme = dest / "README.txt"
    readme.write_text(
        "\n".join(
            [
                "Blind divergence labels for the alignment evaluation set.",
                "",
                "  packets/     one markdown file per pair, anonymous ids, shuffled.",
                "  key.jsonl    maps packet_id → task/run. Do not open during labeling.",
                f"  {LABELS_FILE}  hand labels — write an integer step in `label`.",
                "",
                f"Selection seed {select_seed}; shuffle seed {shuffle_seed}.",
                f"{len(key_rows)} pairs. Definition is at the top of every packet.",
                "",
                "Label:  python -m bench label",
                "Score:  python -m bench align-eval --score",
            ]
        ),
        encoding="utf-8",
    )
    return dest


# ---------------------------------------------------------------------------
# Interactive pass
# ---------------------------------------------------------------------------

_FAILURE_STEPS = re.compile(r"^## FAILURE .*?\((\d+) steps\)", re.MULTILINE)

PROMPT_HELP = """\
  <n>            the failing step after which the run could not recover
  <n> <note>     same, with a note for yourself
  s              skip this packet for now
  ?              print the packet again
  q              save and stop"""


def failure_length(text: str) -> int:
    match = _FAILURE_STEPS.search(text)
    if match is None:
        raise ValueError(
            "packet has no '## FAILURE ... (N steps)' heading, so the label cannot be "
            "range-checked. Re-export the packets."
        )
    return int(match.group(1))


def _sheet_rows(path: Path, packets: Path) -> list[dict]:
    if path.exists():
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if rows:
            return rows
    return [
        {"packet_id": p.stem, "label": None, "note": ""}
        for p in sorted(packets.glob("P*.md"))
    ]


def _write_sheet(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8"
    )


def _parse_entry(answer: str, limit: int) -> tuple[int, str] | None:
    head, _, rest = answer.partition(" ")
    if not head.lstrip("-").isdigit():
        return None
    value = int(head)
    if not 1 <= value <= limit:
        return None
    return value, rest.strip()


def label_interactively(
    dest: Path = LABEL_ROOT,
    shuffle: bool = False,
) -> str:
    """Walk the unlabeled packets one at a time, writing after every answer.

    Nothing about the pair beyond the packet is shown — no task id, no run id,
    no method prediction. Writing as it goes is what makes labeling resumable
    across sittings.
    """
    packets = dest / "packets"
    if not packets.is_dir():
        raise FileNotFoundError(
            f"no packets at {packets}. Run `python -m bench export-labels` first."
        )
    path = dest / LABELS_FILE
    rows = _sheet_rows(path, packets)
    by_id = {r["packet_id"]: r for r in rows}

    pending = [r for r in rows if r["label"] is None]
    if not pending:
        return f"all {len(rows)} packets already labeled in {path}. Nothing to do."
    if shuffle:
        random.Random(SHUFFLE_SEED).shuffle(pending)

    print(f"{len(rows) - len(pending)}/{len(rows)} done, {len(pending)} to go")
    print("Answer with one integer. Commands:")
    print(PROMPT_HELP)

    answered = 0
    for row in pending:
        text = (packets / f"{row['packet_id']}.md").read_text(encoding="utf-8")
        limit = failure_length(text)
        print("\n" + "=" * 76)
        print(text)
        while True:
            done = len(rows) - len(pending) + answered
            try:
                answer = input(
                    f"[{done + 1}/{len(rows)}] {row['packet_id']} — "
                    f"failing step 1-{limit}: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                _write_sheet(path, rows)
                return f"stopped. {done}/{len(rows)} labeled in {path}"
            if answer == "q":
                _write_sheet(path, rows)
                return f"stopped. {done}/{len(rows)} labeled in {path}"
            if answer == "s":
                break
            if answer == "?":
                print(text)
                continue
            parsed = _parse_entry(answer, limit)
            if parsed is None:
                print(f"  need an integer from 1 to {limit}, or s / ? / q")
                continue
            by_id[row["packet_id"]]["label"] = parsed[0]
            by_id[row["packet_id"]]["note"] = parsed[1]
            _write_sheet(path, rows)
            answered += 1
            break

    remaining = sum(1 for r in rows if r["label"] is None)
    return f"{len(rows) - remaining}/{len(rows)} labeled in {path}"
