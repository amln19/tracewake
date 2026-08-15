"""Single-trace divergence localization: commitments, bounds, and reliability."""

from __future__ import annotations

import pytest

from tracewake.align import Step
from tracewake.diverge import (
    RELIABILITY_ACCURACY,
    commitment_steps,
    earliest_bound,
    first_commitment,
    localize,
    novelty_exhausted,
    reliability,
    terminal_repeat,
)


def read(path: str, *, reasoning: str = "") -> Step:
    return Step(name="read", args={"path": path}, target=path, reasoning=reasoning)


def edit(path: str, new: str = "x", *, reasoning: str = "") -> Step:
    return Step(
        name="edit",
        args={"path": path, "new": new},
        target=path,
        reasoning=reasoning,
        writes=frozenset({path}),
    )


def create(path: str, new: str = "x", *, reasoning: str = "") -> Step:
    """A creating action. `edit` presupposes the file was already there."""
    return Step(
        name="create",
        args={"path": path, "new": new},
        target=path,
        reasoning=reasoning,
        writes=frozenset({path}),
    )


# ---------------------------------------------------------------------------
# commitments
# ---------------------------------------------------------------------------


def test_a_run_editing_what_it_created_never_commits():
    # The run brings b.py into existence, then edits what it made. Neither step
    # changes anything that was there before it started, however many times it
    # comes back to the file.
    assert commitment_steps([create("b.py"), edit("b.py", "y")]) == []
    assert commitment_steps([create("b.py")] + [edit("b.py")] * 4) == []


def test_editing_an_unseen_path_commits_because_edit_presupposes_a_file():
    # The verb decides ownership, not novelty. `sed -i` on a file the run never
    # opened is still a change to something that was already there.
    assert commitment_steps([edit("src/thing.py")]) == [1]
    assert commitment_steps([create("scratch.py")]) == []


def test_creating_a_file_the_run_had_already_read_still_commits():
    # Reading first means the file was already there, so writing it changes
    # something that pre-existed the run.
    assert commitment_steps([read("b.py"), create("b.py")]) == [2]


def test_writing_a_path_the_run_read_first_is_a_commitment():
    assert commitment_steps([read("a.py"), edit("a.py")]) == [2]


def test_reads_alone_never_commit():
    assert commitment_steps([read("a.py"), read("b.py")]) == []


def test_first_commitment_needs_no_reference_run():
    bad = [read("a.py"), create("scratch.py"), read("b.py"), edit("b.py")]

    # scratch.py was created by this run, so editing it is not a commitment.
    assert first_commitment(bad) == 4
    assert first_commitment([read("a.py"), read("b.py")]) is None


# ---------------------------------------------------------------------------
# the other two bounds, and their minimum
# ---------------------------------------------------------------------------


def test_terminal_repeat_finds_a_multi_step_cycle():
    """The case `align`'s period-1 guard cannot see."""
    steps = [read("a.py"), read("b.py")] + [read("x.py"), read("y.py")] * 4
    assert terminal_repeat(steps) == 3


def test_terminal_repeat_needs_two_whole_periods():
    assert terminal_repeat([read("a.py"), read("b.py"), read("c.py")]) is None
    assert terminal_repeat([read("a.py"), read("b.py"), read("b.py")]) == 2


def test_terminal_repeat_ignores_a_cycle_that_does_not_reach_the_end():
    """A run that repeated itself and then did something new is not stuck."""
    steps = [read("x.py"), read("x.py"), read("x.py"), edit("done.py")]
    assert terminal_repeat(steps) is None


def test_novelty_exhausted_marks_the_last_once_only_action():
    # r.py is unique and last appears at step 3; everything after repeats.
    steps = [read("a.py"), read("a.py"), read("r.py"), read("b.py"), read("b.py")]
    assert novelty_exhausted(steps) == 4


def test_earliest_bound_takes_the_tightest_of_the_bounds():
    # Commits at 2, then loops from 3 onwards. The commitment is earlier.
    bad = [read("a.py"), edit("a.py")] + [read("z.py"), read("w.py")] * 3
    assert first_commitment(bad) == 2
    assert terminal_repeat(bad) == 3
    assert earliest_bound(bad) == 2


def test_earliest_bound_uses_the_cycle_when_nothing_commits():
    """The class `first_commitment` cannot speak to at all."""
    bad = [read("a.py"), read("b.py")] + [read("x.py"), read("y.py")] * 5
    assert first_commitment(bad) is None
    assert earliest_bound(bad) == 3


def test_earliest_bound_refuses_an_empty_run():
    with pytest.raises(ValueError, match="no steps"):
        earliest_bound([])


def test_earliest_bound_never_exceeds_the_trace():
    bad = [read("a.py"), read("b.py"), read("c.py")]
    assert earliest_bound(bad) <= len(bad)


# ---------------------------------------------------------------------------
# reliability
# ---------------------------------------------------------------------------


def test_reliability_separates_the_class_that_cannot_be_localised():
    short_commit = [read("a.py"), edit("a.py")]
    assert reliability(short_commit) == "commit-short"

    silent_long = [read(f"f{i}.py") for i in range(25)]
    assert reliability(silent_long) == "silent-long"

    long_single = [read("a.py")] + [read(f"f{i}.py") for i in range(20)] + [edit("a.py")]
    assert reliability(long_single) == "commit-long-single"

    long_many = (
        [read("a.py"), edit("a.py")]
        + [read(f"f{i}.py") for i in range(18)]
        + [edit("a.py")]
    )
    assert reliability(long_many) == "commit-long-many"


def test_published_reliability_ordering_stays_monotone():
    """Callers abstain on the tail, so the ordering is the whole point."""
    order = list(RELIABILITY_ACCURACY.values())
    assert order == sorted(order, reverse=True)


def test_localize_reports_a_step_and_how_much_to_trust_it():
    assert localize([read("a.py"), edit("a.py")]) == (2, "commit-short")
