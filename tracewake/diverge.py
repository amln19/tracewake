"""Locate where a failing run went irrecoverably wrong, from that run alone.

`align-v1` reads the divergence off the *last* aligned column that agreed, so
one coincidental late agreement -- two long runs that both view the same file
again at step 44 -- drags the answer to the end of the trace. On externally
labelled RootSE failures that readout lands within two steps of the label on 5
of 58 pairs.

The rule here replaces that readout and drops the successful run entirely.
Reading a file is recoverable; writing one is not, in practice, because these
agents rarely undo. So the run commits at the first step that writes a file it
did not create for itself, and everything before that is finding out.

It needs no reference run, no alignment and no inference, and it was tuned only
on 107 labelled training trajectories, never on RootSE or on any held-out set.

`reliability` reports which of five classes the run falls into, because the
same rule is right about nine times in ten on one class and one in ten on
another. It reports a band, not a percentage: the ordering survives being
re-measured and the percentages do not.

See `contracts/divergence.md` for the measured comparison and the limits.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

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
    steps: Sequence[Step], writes_of=lambda step: set(step.writes)
) -> list[tuple[int, frozenset[str]]]:
    """(1-based index, paths) for each step that changes pre-existing state.

    Two facts are tracked, not one. `seen` is every path the run has referenced;
    `owned` is the subset it brought into existence.

    Both are needed. A run that creates a scratch script and then edits it ten
    times has written a path it referenced earlier every time after the first —
    which looks identical to editing the project's source unless creation is
    remembered separately.

    Ownership is claimed by the *action*, not by novelty. Treating any first
    write to an unseen path as creation — which is what this did originally —
    silently excused every `sed -i` on a file the run had not opened first, and
    left 14 of 58 RootSE failures registering no commitment at all despite up to
    15 writing steps each. Anchoring on the verb takes that to 4 of 58. It is a
    correctness fix and not an accuracy one: within-±2 moved 89 to 90 of 178
    pooled pairs, which is nothing. See `contracts/divergence.md`.
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


SCRATCH_FALLBACK = 12


def first_nonscratch_write(bad: Sequence[Step]) -> int:
    """The step that first writes a file the run did not create for itself.

    A run's steps divide into finding out — reading, searching, running a
    reproduction — and acting on what it thinks it found. The first write to a
    file that was already there is the boundary: it turns a diagnosis from a
    hypothesis into the premise every later step inherits. When the diagnosis is
    wrong what follows is repair, not reconsideration, and the run is already
    lost.

    The carve-out is what makes it work on more than one scaffold. Almost every
    run creates a reproduction script early; counting that as the commitment
    lands a median of thirteen steps early. The scratch file is identified as
    the first path written that was never read — created out of nothing — which
    costs no parameter and no scaffold knowledge.

    Measured on 262 trajectories it had never seen, including all 102 that
    carry externally written labels: 29.4% exact and 50.8% within two steps.
    `contracts/divergence.md` records what it replaced and why.

    `SCRATCH_FALLBACK` is the only fitted number, the median development label,
    and it is inert: sweeping it from 6 to 20 moves held-out exact match between
    29.4% and 29.8%, and parameter-free replacements give 28.6% and 28.2%. It is
    kept at the submitted value rather than swapped, because choosing between
    them on the held-out set would be selecting on the evaluation.
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
            scratch = sorted(written)[0]
        read |= {t for t in step.targets if t and t not in written}
    return min(SCRATCH_FALLBACK, len(bad)) if bad else 1


def _written_paths(step: Step) -> set[str]:
    """Paths this step wrote, falling back to the verb when nothing is derived.

    Adapters populate `Step.writes`; the fallback is insurance for one that does
    not, so the rule stays meaningful on a scaffold this project has not seen.
    On every labelled trajectory the two agree.
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

# How far to trust the answer, as a band rather than a number. The classes hold
# their order across three independent evaluations, which is what makes
# abstaining meaningful; the percentages do not survive being quoted. Measured
# within ±2 on the 262 held-out trajectories: commit-short 88% (n=69),
# commit-long-single 70% (n=23), commit-long-many 36% (n=144), silent-short 29%
# (n=7), silent-long 11% (n=19). The two sparse classes swing by tens of points
# between evaluations and are banded conservatively for that reason.
RELIABILITY_BAND: dict[str, str] = {
    "commit-short": "high",
    "commit-long-single": "moderate",
    "commit-long-many": "low",
    "silent-short": "low",
    "silent-long": "very low",
}
LONG_TRACE = 18


def reliability(bad: Sequence[Step]) -> Reliability:
    """How much to trust the reported step, decided without labels.

    Two things predict whether the answer lands: whether the run committed at
    all, and whether the trace is long. `silent-long` — a long run that never
    changed anything pre-existing — is right about a fifth of the time and
    should be treated as "cannot localise" rather than as an answer.

    The 18-step boundary is `align-v1`'s existing long/short split, reused
    rather than refitted.
    """
    # Uses the inferred writes, not just the derived ones, so the class agrees
    # with the step being reported. `commitment_steps` deliberately does not:
    # the baselines in `bench/` share it, and 130 steps in the labelled corpus
    # carry a write the verb sees and the adapter did not derive, so widening it
    # there would silently restate every published figure.
    commitments = [i for i, _ in _commitments(bad, _written_paths)]
    if not commitments:
        return "silent-long" if len(bad) > LONG_TRACE else "silent-short"
    if len(bad) <= LONG_TRACE:
        return "commit-short"
    return "commit-long-single" if len(commitments) == 1 else "commit-long-many"


def localize(bad: Sequence[Step]) -> tuple[int, Reliability]:
    """Where the failing run went irrecoverably wrong, and how much to trust it.

    The single-trace entry point: no reference run, no alignment, no inference.
    Callers that want to abstain should drop `silent-long`, which is about 14%
    of observed pairs and carries most of the error.

    """
    if not bad:
        raise ValueError("the failure run has no steps to locate a divergence in")
    return first_nonscratch_write(bad), reliability(bad)
