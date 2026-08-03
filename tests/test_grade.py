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
                turns=6,
                actions=5,
                edits=1,
                repeats=1,
                parse_failures=0,
                stop_reason="submitted",
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
                    turns=5,
                    actions=4,
                    edits=1,
                    repeats=0,
                    parse_failures=0,
                    stop_reason="submitted",
                    seconds=10.0,
                    summary="",
                ),
                ledger,
            )
    summary = runner.status(ledger)
    assert "mixed on coverage  1 tasks" in summary
    assert "83.3%" in summary


def test_shards_partition_the_work_without_overlap(tmp_path: Path, monkeypatch) -> None:
    made = [
        Task(
            task_id=f"t{n}",
            repo="thing",
            operator="off_by_one",
            ground_truth_file="pkg/window.py",
            ground_truth_line=2,
            mutation=Mutation("off_by_one", "window.py", 2, 0, 2, 1, "a", "b", ""),
            issue="",
            broken_tests=(),
            baseline_summary="",
            broken_summary="",
        )
        for n in range(7)
    ]
    monkeypatch.setattr(runner, "load", lambda: made)

    planned = [(t, i) for t in made for i in range(3)]
    shards = 3
    covered = [
        {f"{t.task_id}#{i}" for p, (t, i) in enumerate(planned) if p % shards == s}
        for s in range(shards)
    ]
    assert set.union(*covered) == {f"{t.task_id}#{i}" for t, i in planned}
    assert sum(len(c) for c in covered) == len(planned), "a shard claimed work twice"


def test_any_prefix_of_the_job_spans_the_repositories(monkeypatch) -> None:
    """The job is read long before it ends; in task order the first hour is one repo."""
    import random as random_module

    made = [
        Task(
            task_id=f"{repo}-off_by_one-{n}",
            repo=repo,
            operator="off_by_one",
            ground_truth_file="pkg/window.py",
            ground_truth_line=2,
            mutation=Mutation("off_by_one", "window.py", 2, 0, 2, 1, "a", "b", ""),
            issue="",
            broken_tests=(),
            baseline_summary="",
            broken_summary="",
        )
        for repo in ("aaa", "bbb", "ccc", "ddd", "eee", "fff", "ggg", "hhh")
        for n in range(4)
    ]
    planned = [(t, i) for t in made for i in range(5)]
    random_module.Random(runner.ORDER_SEED).shuffle(planned)

    first_twenty = {t.repo for t, _ in planned[:20]}
    assert len(first_twenty) >= 6, f"a 20-attempt prefix only reached {first_twenty}"


def test_the_histogram_separates_a_plateau_from_two_spikes(tmp_path: Path) -> None:
    """The mean cannot tell these apart, and only one of them has pairs."""

    def ledger_for(per_task: dict[str, int], path: Path) -> Path:
        for task_id, successes in per_task.items():
            for index in range(5):
                runner.record_attempt(
                    Attempt(
                        task_id=task_id,
                        run_index=index,
                        run_id=f"{task_id}{index}",
                        coverage=index < successes,
                        resolve=False,
                        turns=5,
                        actions=4,
                        edits=1,
                        repeats=0,
                        parse_failures=0,
                        stop_reason="submitted",
                        seconds=10.0,
                        summary="",
                    ),
                    path,
                )
        return path

    plateau = ledger_for({f"p{n}": 3 for n in range(4)}, tmp_path / "plateau.jsonl")
    bimodal = ledger_for(
        {f"b{n}": (5 if n % 2 else 0) for n in range(4)}, tmp_path / "bimodal.jsonl"
    )

    # Both average 60% and 50% respectively; what matters is the pair count.
    assert "usable pairs: 4 of 4" in runner.status(plateau)
    assert "usable pairs: 0 of 4" in runner.status(bimodal)
    assert "<- no pair" in runner.status(bimodal)


def test_status_reports_how_long_trajectories_are(tmp_path: Path) -> None:
    """Aligning six-action traces cannot show an affine-gap advantage."""
    ledger = tmp_path / "runs.jsonl"
    for index, actions in enumerate((3, 6, 6, 14)):
        runner.record_attempt(
            Attempt(
                task_id="t",
                run_index=index,
                run_id=f"r{index}",
                coverage=True,
                resolve=False,
                turns=actions + 4,
                actions=actions,
                edits=1,
                repeats=4,
                parse_failures=0,
                stop_reason="stuck" if actions < 10 else "submitted",
                seconds=100.0,
                summary="",
            ),
            ledger,
        )
    summary = runner.status(ledger)
    assert "median 6" in summary
    assert "range 3-14" in summary
    assert "ended stuck          3/4" in summary
    assert "spinning" in summary


def test_grading_survives_a_file_the_pinned_checkout_does_not_have(
    tmp_path: Path, task: Task
) -> None:
    """This runs at the end of every attempt in a job that takes hours."""
    root = runner.prepare(task, tmp_path / "work")
    (root / "pkg" / "invented.py").write_text("x = 1\n")

    coverage, resolve, patch = runner.grade(task, root, RED)
    assert coverage is True
    assert "invented.py" in patch


def test_grading_survives_a_file_the_working_copy_lost(tmp_path: Path, task: Task) -> None:
    root = runner.prepare(task, tmp_path / "work")
    (root / "pkg" / "window.py").unlink()

    coverage, resolve, patch = runner.grade(task, root, RED)
    assert "window.py" in patch, "a removed file should still show as a change"


def test_tasks_complete_as_the_job_runs_rather_than_all_at_the_end(monkeypatch) -> None:
    """The gate turns on completed tasks, so a prefix must contain some."""
    import random as random_module

    made = [
        Task(
            task_id=f"{repo}-off_by_one-{n}",
            repo=repo,
            operator="off_by_one",
            ground_truth_file="pkg/w.py",
            ground_truth_line=2,
            mutation=Mutation("off_by_one", "w.py", 2, 0, 2, 1, "a", "b", ""),
            issue="",
            broken_tests=(),
            baseline_summary="",
            broken_summary="",
        )
        for repo in ("aaa", "bbb", "ccc", "ddd", "eee", "fff", "ggg", "hhh")
        for n in range(4)
    ]
    order = list(made)
    random_module.Random(runner.ORDER_SEED).shuffle(order)
    planned = [(t, i) for t in order for i in range(5)]

    prefix = planned[:50]
    complete = [t for t in {p[0].task_id for p in prefix}
                if sum(1 for q in prefix if q[0].task_id == t) == 5]
    assert len(complete) >= 9, f"only {len(complete)} tasks finished in the first 50 attempts"
    assert len({p[0].repo for p in prefix}) >= 5, "a prefix should still span repositories"


def test_the_histogram_reads_the_run_count_off_the_data(tmp_path: Path) -> None:
    """A batch run with three runs per task against a hardcoded five reported
    that no task had finished, hiding the one number the gate turns on."""
    ledger = tmp_path / "runs.jsonl"
    for task_id, successes in {"a": 1, "b": 0, "c": 3}.items():
        for index in range(3):
            runner.record_attempt(
                Attempt(
                    task_id=task_id,
                    run_index=index,
                    run_id=f"{task_id}{index}",
                    coverage=index < successes,
                    resolve=False,
                    turns=5,
                    actions=4,
                    edits=1,
                    repeats=0,
                    parse_failures=0,
                    stop_reason="submitted",
                    seconds=10.0,
                    summary="",
                ),
                ledger,
            )
    summary = runner.status(ledger)
    assert "no task has all" not in summary
    assert "all 3 runs in" in summary
    assert "usable pairs: 1 of 3" in summary
