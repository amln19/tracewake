"""Locate where a failing run went irrecoverably wrong.

The single-trace rule reports the first step that writes a path the run did not
create for itself. It also returns a reliability band based on whether the run
committed and how long the trace is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final, Literal

from .align import Step

# Action names that bring a file into existence. Everything else that writes is
# editing something already there: `sed -i` and `str_replace` presuppose a file,
# whatever the run has bothered to look at first.
CREATE_MARKERS = ("create", "touch", "new_file", "write_file")


def creates_files(step: Step) -> bool:
    """Does this action bring its paths into existence rather than change them?"""
    names = step.names or frozenset({step.name})
    return any(m in n.lower() for n in names for m in CREATE_MARKERS)


def _commitments(
    steps: Sequence[Step],
    writes_of: Callable[[Step], set[str]] = lambda step: set(step.writes),
) -> list[tuple[int, frozenset[str]]]:
    """(1-based index, paths) for each step that changes pre-existing state.

    Two facts are tracked, not one. `seen` is every path the run has referenced;
    `owned` is the subset it brought into existence.

    Both are needed. A run that creates a scratch script and then edits it must
    keep ownership of that path separate from paths that were already present.

    Ownership is claimed by the action, not by novelty. A first write to an
    unseen path is still a commitment unless the action explicitly creates it.
    """
    seen: set[str] = set()
    owned: set[str] = set()
    out: list[tuple[int, frozenset[str]]] = []
    for i, step in enumerate(steps, start=1):
        made = creates_files(step)
        written = writes_of(step)
        changed = frozenset(
            p for p in written if p not in owned and (p in seen or not made)
        )
        if changed:
            out.append((i, changed))
        if made:
            owned |= {p for p in written if p not in seen}
        seen |= set(written)
        seen |= {t for t in step.targets if t}
    return out


def commitment_steps(steps: Sequence[Step]) -> list[int]:
    """1-based indices of steps that modify something the run did not create."""
    return [i for i, _ in _commitments(steps)]


SCRATCH_FALLBACK: Final = 12


def first_nonscratch_write(bad: Sequence[Step]) -> int:
    """The step that first writes a file the run did not create for itself.

    A run's steps divide into finding out — reading, searching, running a
    reproduction — and acting on what it thinks it found. The first write to a
    file that was already there is the boundary: it turns a diagnosis from a
    hypothesis into the premise every later step inherits. When the diagnosis is
    wrong what follows is repair, not reconsideration, and the run is already
    lost.

    The first path written without a prior read is treated as scratch work. A
    later write to another path, or to a path already read, is a commitment.
    """
    read: set[str] = set()
    scratch: str | None = None
    for index, step in enumerate(bad, start=1):
        written = _written_paths(step)
        for path in written:
            # Already read means it predates the run. A second file created from
            # nothing means the first one was the scratch file and this is not.
            if path in read or (scratch is not None and path != scratch):
                return index
        if written and scratch is None:
            scratch = min(written)
        read |= {t for t in step.targets if t and t not in written}
    return min(SCRATCH_FALLBACK, len(bad)) if bad else 1


def _written_paths(step: Step) -> set[str]:
    """Paths this step wrote, falling back to the verb when nothing is derived.

    Adapters populate `Step.writes`; the fallback is insurance for one that does
    not, so the rule remains meaningful when an adapter cannot derive writes
    directly.
    """
    if step.writes:
        return set(step.writes)
    verbs = ("edit", "create", "write", "replace", "insert", "append",
             "patch", "apply", "touch", "new", "save", "sed", "tee", "add")
    out: set[str] = set()
    for name, target in zip(
        step.batch_names or (step.name,), step.batch_targets or (step.target,), strict=False
    ):
        tail = (name or "").split()[0].split(".")[-1].lower() if name else ""
        if target and any(word.startswith(v) for word in tail.replace("-", "_").split("_") for v in verbs):
            out.add(target)
    return out


Reliability = Literal[
    "commit-short", "silent-short", "commit-long-single",
    "commit-long-many", "silent-long",
]
# Not shared with contracts.py's own Confidence: that one is part of a frozen
# wire contract and must not silently follow this module if the local rule's
# classes ever change, per AGENTS.md's versioning invariant.
Confidence = Literal["high", "moderate", "low", "very low"]

# Reliability is a band rather than a score so callers can abstain on the least
# reliable class without depending on a fragile numeric threshold.
RELIABILITY_BAND: dict[Reliability, Confidence] = {
    "commit-short": "high",
    "commit-long-single": "moderate",
    "commit-long-many": "low",
    "silent-short": "low",
    "silent-long": "very low",
}
LONG_TRACE: Final = 18


def reliability(bad: Sequence[Step]) -> Reliability:
    """How much to trust the reported step, decided without labels.

    The band depends on whether the run committed at all and whether the trace
    is long. `silent-long` should be treated as "cannot localise" rather than
    as a precise answer.

    The 18-step boundary is shared with the alignment profile.
    """
    # Use inferred writes here so the reliability class agrees with the step
    # reported by `first_nonscratch_write`.
    commitments = [i for i, _ in _commitments(bad, _written_paths)]
    if not commitments:
        return "silent-long" if len(bad) > LONG_TRACE else "silent-short"
    if len(bad) <= LONG_TRACE:
        return "commit-short"
    return "commit-long-single" if len(commitments) == 1 else "commit-long-many"


def localize(bad: Sequence[Step]) -> tuple[int, Reliability]:
    """Where the failing run went irrecoverably wrong, and how much to trust it.

    The single-trace entry point: no reference run, no alignment, no inference.
    Callers that cannot use an uncertain answer should drop `silent-long`.
    """
    if not bad:
        raise ValueError("the failure run has no steps to locate a divergence in")
    return first_nonscratch_write(bad), reliability(bad)
