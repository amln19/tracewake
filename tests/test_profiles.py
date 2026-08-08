from __future__ import annotations

from tracewake.align import Step
from tracewake.profiles import LEXICAL_V1, lexical_v1_align


def test_lexical_v1_parameters_are_frozen() -> None:
    assert LEXICAL_V1.model_dump(mode="json") == {
        "name": "lexical-v1",
        "version": 1,
        "token_pattern": "[A-Za-z0-9_./-]+",
        "case": "lower",
        "blank_token": ".",
        "weights": {"tool": 0.45, "args": 0.25, "reasoning": 0.2, "files": 0.1},
        "argument_weights": {"target": 0.7, "rest": 0.3},
        "line_falloff": 50.0,
        "gap_open": -1.0,
        "gap_extend": -0.2,
        "score_transform": "2*s-1",
        "divergence_rule": "last-target-agreement",
    }


def test_lexical_v1_alignment_has_a_frozen_golden_result() -> None:
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

    result = lexical_v1_align(good, bad)

    assert result.alignment == [(0, 0), (1, 1), (2, 2)]
    assert result.divergence is None
    assert result.score == 1.2
