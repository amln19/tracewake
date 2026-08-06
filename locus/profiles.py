from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .align import (
    DEFAULT_CONFIG,
    Aligned,
    LexicalEmbedder,
    Step,
    align,
    divergence_step,
)
from .contracts import AnalysisProfile

LEXICAL_V1 = AnalysisProfile(
    name="lexical-v1",
    version=1,
    token_pattern=r"[A-Za-z0-9_./-]+",
    case="lower",
    blank_token=".",
    weights={"tool": 0.45, "args": 0.25, "reasoning": 0.20, "files": 0.10},
    argument_weights={"target": 0.70, "rest": 0.30},
    line_falloff=50.0,
    gap_open=-1.0,
    gap_extend=-0.2,
    score_transform="2*s-1",
    divergence_rule="last-target-agreement",
)


@dataclass(frozen=True)
class LexicalAlignment:
    alignment: Aligned
    score: float
    divergence: int | None
    scores: list[list[float]]


def lexical_v1_align(good: Sequence[Step], bad: Sequence[Step]) -> LexicalAlignment:
    total, pairs, scores = align(
        good,
        bad,
        embed=LexicalEmbedder(),
        config=DEFAULT_CONFIG,
    )
    return LexicalAlignment(
        alignment=pairs,
        score=total,
        divergence=divergence_step(pairs, good, bad),
        scores=scores,
    )
