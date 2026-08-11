"""Readers and pairing rules for published trajectories."""

from __future__ import annotations

import json
from pathlib import Path

from bench.external import (
    ExternalPair,
    _read_jsonl,
    export_openhands_packets,
    instruction_kind,
    load_openhands_pairs,
    model_of_run_id,
    score_openhands_labels,
    select_openhands_pairs,
    strip_terminal,
    to_steps,
)
from tracewake.align import Step


def _assistant(tool_name: str, args: dict, content: str = "thinking") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }


def test_openhands_editor_and_bash_become_steps() -> None:
    messages = [
        {"role": "user", "content": "fix the bug in /workspace/pkg"},
        _assistant("str_replace_editor", {"command": "view", "path": "/workspace/pkg/a.py"}),
        _assistant("execute_bash", {"command": "pytest tests/test_a.py -q"}),
        _assistant("finish", {"message": "done"}),
    ]
    steps = strip_terminal(to_steps(messages, shell_verbs=True))
    assert [s.name for s in steps] == ["str_replace_editor.view", "pytest"]
    assert steps[0].target == "/workspace/pkg/a.py"
    assert steps[1].target == "tests/test_a.py"


def test_finish_and_submit_are_stripped_as_terminals() -> None:
    steps = [
        Step("read", {}, target="a.py"),
        Step("finish", {}),
    ]
    assert [s.name for s in strip_terminal(steps)] == ["read"]
    assert [s.name for s in strip_terminal([Step("submit", {})])] == []


def test_swe_smith_instruction_kind_separates_bug_report() -> None:
    fix = [{"role": "user", "content": "Please fix the issue in the repo."}]
    report = [{"role": "user", "content": "Write example_bug_report.md for this issue."}]
    assert instruction_kind(fix) == "fix_source"
    assert instruction_kind(report) == "bug_report"


def test_model_of_run_id_reads_openhands_prefix() -> None:
    assert (
        model_of_run_id("gpt-4o-2024-08-06_maxiter_30_N_v2.1-no-hint-train-t04-run_1")
        == "gpt-4o-2024-08-06"
    )


def test_load_openhands_pairs_requires_same_model_mixed_outcome() -> None:
    def traj(*names: str) -> list[dict]:
        return [
            {"role": "user", "content": "fix pr"},
            *[_assistant("str_replace_editor", {"command": "view", "path": f"{n}.py"}) for n in names],
            _assistant("finish", {}),
        ]

    rows = [
        {
            "instance_id": "pkg-1",
            "run_id": "gpt-4o-2024-08-06_maxiter_1-run_1",
            "resolved": True,
            "messages": traj("a", "b", "c"),
            "model": "gpt-4o-2024-08-06",
        },
        {
            "instance_id": "pkg-1",
            "run_id": "gpt-4o-2024-08-06_maxiter_1-run_2",
            "resolved": False,
            "messages": traj("a", "x", "y"),
            "model": "gpt-4o-2024-08-06",
        },
        {
            "instance_id": "pkg-1",
            "run_id": "claude_maxiter_1-run_1",
            "resolved": False,
            "messages": traj("a", "z"),
            "model": "claude",
        },
        {
            "instance_id": "pkg-2",
            "run_id": "gpt-4o-2024-08-06_maxiter_1-run_3",
            "resolved": False,
            "messages": traj("a"),
            "model": "gpt-4o-2024-08-06",
        },
    ]
    pairs = load_openhands_pairs(rows=rows, model="gpt-4o-2024-08-06")
    assert len(pairs) == 1
    assert pairs[0].instance_id == "pkg-1"
    assert pairs[0].good_run_id.endswith("run_1")
    assert pairs[0].bad_run_id.endswith("run_2")


def _fixture_pairs(count: int) -> list[ExternalPair]:
    return [
        ExternalPair(f"i{i}", "m", (Step("a", {}),), (Step("b", {}),), f"g{i}", f"b{i}")
        for i in range(count)
    ]


def test_select_openhands_pairs_is_seeded() -> None:
    pairs = _fixture_pairs(10)
    a = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=1)]
    b = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=1)]
    c = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=2)]
    assert a == b
    assert a != c


def test_a_bigger_selection_is_a_superset_of_a_smaller_one() -> None:
    """What lets a labeled sheet grow: the extra pairs are added, not reshuffled."""
    pairs = _fixture_pairs(20)
    small = [p.instance_id for p in select_openhands_pairs(pairs, n=5, seed=7)]
    large = [p.instance_id for p in select_openhands_pairs(pairs, n=12, seed=7)]
    assert large[:5] == small


def test_scoring_tells_apart_packets_that_share_a_run_id(tmp_path: Path) -> None:
    """`run_id` is the sampling config, not a row id — 5.8k rollouts share eight.

    Keyed on run id alone every packet scores against the same trajectories,
    which looks like a working eval and measures nothing.
    """
    def traj(*names: str) -> list[dict]:
        return [
            {"role": "user", "content": "fix pr"},
            *[
                _assistant("str_replace_editor", {"command": "view", "path": f"{n}.py"})
                for n in names
            ],
        ]

    good_id, bad_id = "cfg-t0-run_1", "cfg-t1-run_1"
    rows = [
        {"instance_id": "pkg-1", "run_id": good_id, "resolved": True,
         "messages": traj("a", "b", "c", "d"), "model": "m"},
        {"instance_id": "pkg-1", "run_id": bad_id, "resolved": False,
         "messages": traj("a", "b", "c", "z"), "model": "m"},
        {"instance_id": "pkg-2", "run_id": good_id, "resolved": True,
         "messages": traj("q", "r", "s", "t"), "model": "m"},
        {"instance_id": "pkg-2", "run_id": bad_id, "resolved": False,
         "messages": traj("x", "y"), "model": "m"},
    ]
    keys = [
        {"packet_id": "E01", "instance_id": "pkg-1", "good_run_id": good_id,
         "bad_run_id": bad_id},
        {"packet_id": "E02", "instance_id": "pkg-2", "good_run_id": good_id,
         "bad_run_id": bad_id},
    ]
    (tmp_path / "key.jsonl").write_text(
        "".join(json.dumps(k) + "\n" for k in keys), encoding="utf-8"
    )
    (tmp_path / "labels.jsonl").write_text(
        '{"packet_id": "E01", "label": 4}\n{"packet_id": "E02", "label": 1}\n',
        encoding="utf-8",
    )
    out = tmp_path / "predictions.jsonl"
    score_openhands_labels(
        labels_path=tmp_path / "labels.jsonl",
        key_path=tmp_path / "key.jsonl",
        model="m",
        out=out,
        rows=rows,
    )
    generated = _read_jsonl(out)
    assert generated[0]["_meta"]["labels"] == "labels.jsonl"
    scored = generated[1:]
    assert [r["packet_id"] for r in scored] == ["E01", "E02"]
    assert scored[0]["aligner"] != scored[1]["aligner"], (
        "both packets scored against the same trajectories"
    )


def test_the_score_report_never_states_a_hit_rate_without_its_ceiling(
    tmp_path: Path,
) -> None:
    """A constant answer scores well when labels cluster near the start.

    Reporting the aligner's rate alone invites quoting it as a win, so every
    group in the report carries the best-constant rate beside it.
    """
    def traj(*names: str) -> list[dict]:
        return [
            {"role": "user", "content": "fix pr"},
            *[
                _assistant("str_replace_editor", {"command": "view", "path": f"{n}.py"})
                for n in names
            ],
        ]

    rows = [
        {"instance_id": "pkg-1", "run_id": "g", "resolved": True,
         "messages": traj("a", "b", "c", "d"), "model": "m"},
        {"instance_id": "pkg-1", "run_id": "b", "resolved": False,
         "messages": traj("a", "b", "z"), "model": "m"},
    ]
    (tmp_path / "key.jsonl").write_text(
        json.dumps(
            {"packet_id": "E01", "instance_id": "pkg-1",
             "good_run_id": "g", "bad_run_id": "b"}
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "labels.jsonl").write_text(
        '{"packet_id": "E01", "label": 3}\n', encoding="utf-8"
    )
    report = score_openhands_labels(
        labels_path=tmp_path / "labels.jsonl",
        key_path=tmp_path / "key.jsonl",
        model="m",
        out=tmp_path / "predictions.jsonl",
        rows=rows,
    )
    lines = report.splitlines()
    aligner_lines = [l for l in lines if l.startswith("  aligner ") and "within±2" in l]
    oracle_lines = [l for l in lines if l.startswith("  oracle_k ")]
    assert aligner_lines, report
    assert len(oracle_lines) == len(aligner_lines), report
    assert "McNemar vs baseline_a" in report, report


def test_extending_the_sheet_keeps_packet_ids_and_filled_labels(tmp_path: Path) -> None:
    pairs = _fixture_pairs(9)
    export_openhands_packets(n=3, seed=7, dest=tmp_path, pairs=pairs)
    key_before = _read_jsonl(tmp_path / "key.jsonl")
    (tmp_path / "labels.jsonl").write_text(
        "".join(
            json.dumps({"packet_id": row["packet_id"], "label": i + 1, "note": "x"}) + "\n"
            for i, row in enumerate(key_before)
        ),
        encoding="utf-8",
    )

    export_openhands_packets(n=6, seed=7, dest=tmp_path, pairs=pairs, extend=True)

    key_after = _read_jsonl(tmp_path / "key.jsonl")
    labels_after = _read_jsonl(tmp_path / "labels.jsonl")
    assert key_after[:3] == key_before, "an extension repointed an already-labeled packet"
    assert [row["label"] for row in labels_after] == [1, 2, 3, None, None, None]
    assert [row["packet_id"] for row in key_after[3:]] == ["E04", "E05", "E06"]
    assert len(list((tmp_path / "packets").glob("*.md"))) == 6
