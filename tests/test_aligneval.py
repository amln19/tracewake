from __future__ import annotations

from bench.aligneval import (
    _parse_judge_label,
    mcnemar,
    oracle_constant,
    within_tol,
)


def test_parse_judge_label_takes_first_in_range():
    assert _parse_judge_label("4", 1, 16) == 4
    assert _parse_judge_label("The step is 7.", 1, 16) == 7
    assert _parse_judge_label("99", 1, 16) is None
    assert _parse_judge_label("no number", 1, 16) is None


def test_mcnemar_counts_discordant_pairs():
    a = [True, True, False, False, True]
    b = [True, False, True, False, False]
    b_only, a_only, p = mcnemar(a, b)
    assert a_only == 2
    assert b_only == 1
    assert p is not None


def test_oracle_constant_prefers_the_mode_band():
    labels = [4, 4, 4, 5, 5, 16]
    assert oracle_constant(labels) == 4
    assert within_tol(4, 5)
