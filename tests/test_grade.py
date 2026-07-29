"""Grading: what counts as a patch, and what counts as a fix."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import repos, runner
from bench.repos import Repo, SuiteReport
from bench.runner import Attempt
from bench.tasks import Mutation, Task


CLEAN = "def slice_window(xs, i, n):\n    return xs[i : i + n]\n"
BROKEN = "def slice_window(xs, i, n):\n    return xs[i : i + n + 1]\n"
TEST_FILE = "def test_edge():\n    assert True\n"


@pytest.fixture
def pristine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The pinned checkout, which grading diffs against."""
    root = tmp_path / "thing"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "window.py").write_text(CLEAN, encoding="utf-8")
    (root / "pkg" / "test_window.py").write_text(TEST_FILE, encoding="utf-8")
    monkeypatch.setattr(repos, "CLONE_ROOT", tmp_path)
    monkeypatch.setitem(runner.BY_NAME, "thing", Repo("thing", "", "", ("pkg",)))
    return root


@pytest.fixture
def task(pristine: Path) -> Task:
    bound = CLEAN.index("i + n", CLEAN.index("xs[") + 4)
    column = bound - CLEAN.index("    return")
    return Task(
        task_id="thing-off_by_one-1",
        repo="thing",
        operator="off_by_one",
        ground_truth_file="pkg/window.py",
        ground_truth_line=2,
        mutation=Mutation(
            operator="off_by_one",
            file="window.py",
            line=2,
            column=column,
            end_line=2,
            end_column=column + len("i + n"),
            before="i + n",
            after="i + n + 1",
            description="bound shifted",
        ),
        issue="off by one",
        broken_tests=("t.py::test_edge",),
        baseline_summary="4 passed",
        broken_summary="1 failed, 3 passed",
    )


GREEN = SuiteReport(passed=4, returncode=0, summary="4 passed")
RED = SuiteReport(passed=3, failed=1, returncode=1, summary="1 failed, 3 passed")


def test_the_working_copy_starts_with_the_bug_in_it(tmp_path: Path, task: Task) -> None:
    root = runner.prepare(task, tmp_path / "work")
    assert (root / "pkg" / "window.py").read_text() == BROKEN


def test_an_untouched_run_has_neither_coverage_nor_resolve(tmp_path: Path, task: Task) -> None:
    root = runner.prepare(task, tmp_path / "work")
    coverage, resolve, patch = runner.grade(task, root, RED)
    assert (coverage, resolve) == (False, False)
    assert patch == ""


def test_a_wrong_but_well_formed_edit_gets_coverage_without_resolve(
    tmp_path: Path, task: Task
) -> None:
    root = runner.prepare(task, tmp_path / "work")
    target = root / "pkg" / "window.py"
    target.write_text("def slice_window(xs, i, n):\n    return xs[i : i + n + 2]\n")

    coverage, resolve, patch = runner.grade(task, root, RED)
    assert (coverage, resolve) == (True, False)
    assert "+    return xs[i : i + n + 2]" in patch


def test_the_correct_fix_gets_both(tmp_path: Path, task: Task) -> None:
    root = runner.prepare(task, tmp_path / "work")
    (root / "pkg" / "window.py").write_text(CLEAN)
    coverage, resolve, patch = runner.grade(task, root, GREEN)
    assert (coverage, resolve) == (True, True)


def test_an_edit_that_does_not_parse_is_not_a_patch(tmp_path: Path, task: Task) -> None:
    root = runner.prepare(task, tmp_path / "work")
    (root / "pkg" / "window.py").write_text("def slice_window(xs, i, n)\n    return xs[\n")
    coverage, resolve, _ = runner.grade(task, root, RED)
    assert (coverage, resolve) == (False, False)


def test_a_run_that_only_edited_a_test_file_gets_no_coverage(
    tmp_path: Path, task: Task
) -> None:
    root = runner.prepare(task, tmp_path / "work")
    (root / "pkg" / "test_window.py").write_text("def test_edge():\n    assert False\n")
    coverage, resolve, _ = runner.grade(task, root, GREEN)
    assert (coverage, resolve) == (False, False)


def test_the_ledger_skips_what_it_already_holds(tmp_path: Path) -> None:
    ledger = tmp_path / "runs.jsonl"
    for index in (0, 1):
        runner.record_attempt(
            Attempt(
                task_id="a-1",
                run_index=index,
                run_id=f"r{index}",
                coverage=True,
                resolve=False,
                steps=5,
                edits=1,
                parse_failures=0,
                seconds=42.0,
                summary="1 failed",
            ),
            ledger,
        )
    assert runner.done(ledger) == {"a-1#0", "a-1#1"}
    assert len(ledger.read_text().splitlines()) == 2


def test_status_reports_the_rate_and_which_tasks_came_out_mixed(tmp_path: Path) -> None:
    ledger = tmp_path / "runs.jsonl"
    outcomes = {"mixed": [True, False, True], "always": [True, True, True]}
    for task_id, results in outcomes.items():
        for index, coverage in enumerate(results):
            runner.record_attempt(
                Attempt(
                    task_id=task_id,
                    run_index=index,
                    run_id=f"{task_id}{index}",
                    coverage=coverage,
                    resolve=False,
                    steps=4,
                    edits=1,
                    parse_failures=0,
                    seconds=10.0,
                    summary="",
                ),
                ledger,
            )
    summary = runner.status(ledger)
    assert "mixed on coverage  1 tasks" in summary
    assert "83.3%" in summary
