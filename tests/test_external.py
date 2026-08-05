"""Readers and pairing rules for published trajectories."""

from __future__ import annotations

import json

from bench.external import (
    ExternalPair,
    instruction_kind,
    load_openhands_pairs,
    model_of_run_id,
    select_openhands_pairs,
    strip_terminal,
    to_steps,
)
from locus.align import Step


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


def test_select_openhands_pairs_is_seeded() -> None:
    pairs = [
        ExternalPair(f"i{i}", "m", (Step("a", {}),), (Step("b", {}),), f"g{i}", f"b{i}")
        for i in range(10)
    ]
    a = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=1)]
    b = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=1)]
    c = [p.instance_id for p in select_openhands_pairs(pairs, n=3, seed=2)]
    assert a == b
    assert a != c
