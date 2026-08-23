from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .align import (
    DEFAULT_CONFIG,
    Aligned,
    LexicalEmbedder,
    Step,
    align,
)
from .contracts import AnalysisProfile
from .diverge import localize

ALIGN_V2 = AnalysisProfile(
    name="align-v2",
    version=2,
    token_pattern=r"[A-Za-z0-9_./-]+",
    case="lower",
    blank_token=".",
    weights={"tool": 0.45, "args": 0.25, "reasoning": 0.20, "files": 0.10},
    argument_weights={"target": 0.70, "rest": 0.30},
    line_falloff=50.0,
    gap_open=-1.0,
    gap_extend=-0.2,
    score_transform="2*s-1",
    divergence_rule="first-nonscratch-write",
)


@dataclass(frozen=True)
class ProfileAlignment:
    alignment: Aligned
    score: float
    divergence: int | None
    scores: list[list[float]]


def align_v2(good: Sequence[Step], bad: Sequence[Step]) -> ProfileAlignment:
    """Align two runs, and report where the failing one went wrong.

    The alignment is `align-v1`'s, unchanged. The divergence is not: it comes
    from the single-trace rule, which answers the question the alignment readout
    was being asked and could not answer well. An empty failing run has no step
    to report, and the alignment is still meaningful without one.
    """
    total, pairs, scores = align(
        good,
        bad,
        embed=LexicalEmbedder(),
        config=DEFAULT_CONFIG,
    )
    return ProfileAlignment(
        alignment=pairs,
        score=total,
        divergence=localize(bad)[0] if bad else None,
        scores=scores,
    )

