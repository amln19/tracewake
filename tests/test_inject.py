from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bench.tasks import (
    Mutation,
    apply_mutation,
    candidates,
    relative_source_files,
    write_issue,
)
from bench.repos import Repo, _parse


def _mutations(source: str, operator: str | None = None) -> list[tuple[Mutation, str]]:
    found = candidates(Path("thing.py"), source)
    return [(m, s) for m, s in found if operator is None or m.operator == operator]


def test_a_comparison_swap_changes_only_the_operator() -> None:
    source = "def f(a, b):\n    return a < b\n"
    (mutation, mutated), = _mutations(source, "operator_swap")
    assert mutated == "def f(a, b):\n    return a <= b\n"
    assert mutation.before == "a < b"
    assert mutation.line == 2


def test_an_operator_inside_a_string_is_not_touched() -> None:
    source = 'def f(a, b):\n    label = "a < b"\n    return a == b\n'
    for _, mutated in _mutations(source, "operator_swap"):
        assert 'label = "a < b"' in mutated


def test_off_by_one_shifts_a_slice_bound() -> None:
    source = "def f(xs, i, n):\n    return xs[i : i + n]\n"
    produced = {mutated for _, mutated in _mutations(source, "off_by_one")}
    assert "def f(xs, i, n):\n    return xs[i + 1 : i + n]\n" in produced


def test_an_integer_bound_is_folded_rather_than_appended() -> None:
    source = "def f(xs):\n    return xs[3]\n"
    produced = {mutated for _, mutated in _mutations(source, "off_by_one")}
    assert "def f(xs):\n    return xs[4]\n" in produced
    assert "def f(xs):\n    return xs[2]\n" in produced


def test_argument_swap_exchanges_the_first_two_arguments() -> None:
    source = "def f():\n    return replace(text, pattern)\n"
    (_, mutated), = _mutations(source, "argument_swap")
    assert mutated == "def f():\n    return replace(pattern, text)\n"


def test_a_guard_clause_is_replaced_by_pass() -> None:
    source = "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n"
    (_, mutated), = _mutations(source, "deleted_guard")
    assert mutated == "def f(x):\n    pass\n    return x + 1\n"
    ast.parse(mutated)


def test_every_mutation_a_file_admits_still_parses() -> None:
    source = Path("bench/tasks.py").read_text(encoding="utf-8")
    produced = candidates(Path("tasks.py"), source)
    assert len(produced) > 50
    for mutation, mutated in produced:
        ast.parse(mutated)


def test_a_mutation_reapplies_at_the_offsets_it_recorded() -> None:
    source = "def f(a, b):\n    return a < b\n"
    (mutation, mutated), = _mutations(source, "operator_swap")
    assert apply_mutation(source, mutation) == mutated


def test_reapplying_to_changed_source_refuses_rather_than_corrupting() -> None:
    source = "def f(a, b):\n    return a < b\n"
    (mutation, _), = _mutations(source, "operator_swap")
    with pytest.raises(RuntimeError, match="no longer applies"):
        apply_mutation("def f(a, b):\n    return b\n", mutation)


def test_an_issue_never_names_the_file_or_the_line() -> None:
    repo = Repo(
        name="thing", url="", commit="", source_dirs=("thing",),
    )
    mutation = Mutation(
        operator="off_by_one",
        file="window.py",
        line=53,
        column=4,
        end_line=53,
        end_column=14,
        before="max_length",
        after="max_length + 1",
        description="bound shifted",
    )
    issue = write_issue(repo, mutation, ("tests/test_window.py::TestWindow::test_edge",))
    assert "window.py" not in issue
    assert "53" not in issue
    assert "max_length" not in issue
    assert "test_edge" in issue


def test_an_issue_lists_each_failing_test_once() -> None:
    repo = Repo(name="thing", url="", commit="", source_dirs=("thing",))
    mutation = Mutation(
        operator="operator_swap",
        file="a.py",
        line=1,
        column=0,
        end_line=1,
        end_column=1,
        before="<",
        after="<=",
        description="",
    )
    failing = ("a.py::One::test_same", "b.py::One::test_same", "c.py::Two::test_other")
    assert write_issue(repo, mutation, failing).count("One::test_same") == 1


def test_source_listing_skips_caches(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "a.py").write_text("")
    assert relative_source_files(tmp_path, ("pkg",)) == ["pkg/a.py"]


def test_pytest_summary_parsing_names_each_failing_test() -> None:
    output = (
        "FAILED test.py::TestOne::test_a - ValueError: nope\n"
        "FAILED test.py::TestOne::test_b - AssertionError\n"
        "2 failed, 80 passed in 0.28s\n"
    )
    report = _parse(output, 1)
    assert report.failing_tests == ("test.py::TestOne::test_a", "test.py::TestOne::test_b")
    assert (report.passed, report.failed) == (80, 2)
    assert not report.green


def test_a_green_summary_parses_as_green() -> None:
    report = _parse("82 passed in 0.31s\n", 0)
    assert report.green
    assert report.passed == 82
    assert report.failing_tests == ()
