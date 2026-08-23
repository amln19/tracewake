from __future__ import annotations

from tracewake.align import Step
from tracewake.profiles import ALIGN_V2, LOCALIZE_V1, align_v2, localize_v1


def test_align_v2_parameters_are_frozen() -> None:
    assert ALIGN_V2.model_dump(mode="json") == {
        "name": "align-v2",
        "version": 2,
        "token_pattern": "[A-Za-z0-9_./-]+",
        "case": "lower",
        "blank_token": ".",
        "weights": {"tool": 0.45, "args": 0.25, "reasoning": 0.2, "files": 0.1},
        "argument_weights": {"target": 0.7, "rest": 0.3},
        "line_falloff": 50.0,
        "gap_open": -1.0,
        "gap_extend": -0.2,
        "score_transform": "2*s-1",
        "divergence_rule": "first-nonscratch-write",
    }


def test_localize_v1_parameters_are_frozen() -> None:
    assert LOCALIZE_V1.model_dump(mode="json") == {
        "name": "localize-v1",
        "version": 1,
        "rule": "first-nonscratch-write",
        "create_markers": ["create", "touch", "new_file", "write_file"],
        "scratch_fallback": 12,
        "long_trace": 18,
    }


def _pair() -> tuple[list[Step], list[Step]]:
    good = [
        Step(name="read", args={"path": "src/a.py"}, target="src/a.py", reasoning="inspect guard"),
        Step(name="edit", args={"path": "src/a.py", "new": "fixed"}, target="src/a.py", reasoning="apply fix"),
        Step(name="test", args={"path": "tests/test_a.py"}, target="tests/test_a.py", reasoning="run tests"),
    ]
    bad = [
        Step(name="read", args={"path": "src/a.py"}, target="src/a.py", reasoning="inspect guard"),
        Step(name="search", args={"query": "other"}, target="other", reasoning="look elsewhere"),
        Step(name="test", args={"path": "tests/test_a.py"}, target="tests/test_a.py", reasoning="run tests"),
    ]
    return good, bad


def test_align_v2_has_a_frozen_golden_alignment() -> None:
    good, bad = _pair()

    result = align_v2(good, bad)

    # The alignment is align-v1's, unchanged: same columns, same score.
    assert result.alignment == [(0, 0), (1, 1), (2, 2)]
    assert result.score == 1.2


def test_align_v2_divergence_is_the_single_trace_rule() -> None:
    """align-v1 read this off the alignment and reported nothing here.

    The failing run writes nothing, so the rule falls back to the scratch
    bound clamped to the trace length rather than declining to answer. The
    reliability class is what says not to trust it.
    """
    good, bad = _pair()

    assert align_v2(good, bad).divergence == 3


def test_localize_v1_needs_no_reference_run() -> None:
    _, bad = _pair()

    result = localize_v1(bad)

    assert (result.step, result.step_count) == (3, 3)
    assert result.reliability == "silent-short"
