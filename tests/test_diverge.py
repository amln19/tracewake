"""Single-trace divergence localization: commitments, bounds, and reliability."""

from __future__ import annotations

import pytest

from tracewake.align import Step
from bench.baselines import (
    earliest_bound,
    first_commitment,
    novelty_exhausted,
    terminal_repeat,
)
from tracewake.diverge import (
    RELIABILITY_BAND,
    SCRATCH_FALLBACK,
    commitment_steps,
    first_nonscratch_write,
    localize,
    reliability,
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


def test_earliest_bound_covers_a_loop_through_novelty_not_repetition():
    """The class `first_commitment` cannot speak to at all.

    A run that loops to the end is still bounded, but repetition is not what
    bounds it: periodicity from step k implies every action from k on occurs
    twice, so novelty is exhausted no later than k. This is why `earliest_bound`
    does not consult `terminal_repeat` — it can never be the strict minimum.
    """
    bad = [read("a.py"), read("b.py")] + [read("x.py"), read("y.py")] * 5
    assert first_commitment(bad) is None
    assert terminal_repeat(bad) == 3
    assert novelty_exhausted(bad) == 3
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


def test_every_class_carries_a_confidence_band():
    """Callers abstain on the tail, so every class has to say where it sits.

    The bands replaced per-class percentages: the ordering survives being
    re-measured on fresh data and the percentages do not, so quoting one was
    false precision on a figure with a twelve-point interval.
    """
    from tracewake.diverge import Reliability
    import typing
    assert set(RELIABILITY_BAND) == set(typing.get_args(Reliability))
    assert RELIABILITY_BAND["commit-short"] == "high"
    assert RELIABILITY_BAND["silent-long"] == "very low"


def test_localize_reports_a_step_and_how_much_to_trust_it():
    assert localize([read("a.py"), edit("a.py")]) == (2, "commit-short")


def test_the_two_rules_decide_ownership_differently():
    """Where the superseded bound and the reported rule actually diverge.

    Both exclude a scratch file; they disagree on how one is recognised.
    `first_commitment` asks the verb — `edit` presupposes a file that was
    already there, so writing an unseen path counts immediately. The reported
    rule asks the run's own history — a path it never read is one it must have
    made, whatever verb it used.

    Here the run writes its reproduction script with `edit` rather than
    `create`. The old bound calls that the commitment at step 1; the new rule
    waits for step 3, the write to a file it had actually looked at.
    """
    bad = [edit("repro.py"), read("src/a.py"), edit("src/a.py")]
    assert first_commitment(bad) == 1
    assert earliest_bound(bad) == 1
    assert first_nonscratch_write(bad) == 3
    assert localize(bad)[0] == 3


def test_the_scratch_rule_falls_back_when_a_run_never_writes():
    bad = [read("a.py")] * 30
    assert first_nonscratch_write(bad) == SCRATCH_FALLBACK


def test_the_scratch_rule_infers_writes_when_none_are_derived():
    """Insurance for an adapter that ships no `writes`: the verb still reads."""
    bare = [
        Step(name="str_replace_editor.view", args={}, target="src/a.py"),
        Step(name="str_replace_editor.str_replace", args={}, target="src/a.py"),
    ]
    assert not any(s.writes for s in bare)
    assert first_nonscratch_write(bare) == 2


def test_observations_do_not_enter_the_bounds():
    """`Step.observation` is carried for adapters and future signals only.

    Measured on the development halves, observation-based bounds added nothing,
    so nothing in `diverge` reads the field. This pins that: attaching an
    observation must not move an answer.
    """
    plain = [read("a.py"), edit("a.py")] + [read("z.py")] * 4
    noisy = [
        Step(name=s.name, args=s.args, target=s.target, writes=s.writes,
             observation="Traceback (most recent call last): boom")
        for s in plain
    ]
    assert earliest_bound(noisy) == earliest_bound(plain)
    assert reliability(noisy) == reliability(plain)
