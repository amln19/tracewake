"""Locate where a failing run went irrecoverably wrong, from that run alone.

`align-v1` reads the divergence off the *last* aligned column that agreed, so
one coincidental late agreement -- two long runs that both view the same file
again at step 44 -- drags the answer to the end of the trace. On externally
labelled RootSE failures that readout lands within two steps of the label on 5
of 58 pairs.

The rule here replaces that readout and drops the successful run entirely.
Reading a file is recoverable; writing one is not, in practice, because these
agents rarely undo. Two facts each bound the point of no return from above:

  * the run changed something it did not create;
  * it stopped doing anything it does not also repeat.

Each says "no later than this", so the earliest is the tightest bound. On the
same RootSE pairs that reaches 27 of 58, and it needs no reference run, no
alignment and no inference.

Terminal repetition was a third of these and is not one any more: periodicity
to the end implies novelty exhaustion no later, so it can never be the strict
minimum. See `terminal_repeat`.

`reliability` reports which of five classes the run falls into, because the
same rule is right 87% of the time on one class and 21% on another.

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


def _commitments(steps: Sequence[Step]) -> list[tuple[int, frozenset[str]]]:
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
        changed = frozenset(
            p for p in step.writes if p not in owned and (p in seen or not made)
        )
        if changed:
            out.append((i, changed))
        if made:
            owned |= {p for p in step.writes if p not in seen}
        seen |= set(step.writes)
        seen |= {t for t in step.targets if t}
    return out


def commitment_steps(steps: Sequence[Step]) -> list[int]:
    """1-based indices of steps that modify something the run did not create."""
    return [i for i, _ in _commitments(steps)]


def first_commitment(bad: Sequence[Step]) -> int | None:
    """The failing run's first irreversible step.

    Asking instead which commitments a *successful* run did not also make needs
    a reference, and was measured to be worth about ten points on a same-model
    pair and about seven points of harm against a reference from a different
    model, because the alignment then excuses genuine commitments and invents
    differences that are only model idiom. That variant was withdrawn; see
    `contracts/divergence.md`.
    """
    steps = commitment_steps(bad)
    return steps[0] if steps else None


def terminal_repeat(steps: Sequence[Step]) -> int | None:
    """1-based index where the run's terminal repeating cycle begins, or None.

    A run whose action sequence becomes exactly periodic and stays periodic to
    the end cannot recover: it is emitting the same actions forever. So the
    start of that cycle bounds the point of no return from above. This is a
    definition, not a fitted heuristic — which is why it takes no threshold
    beyond "at least two whole periods", the minimum that makes a period mean
    anything.

    `earliest_bound` does not use it, because it cannot ever be the tightest of
    the bounds. If the actions are exactly periodic from step `k` to the end
    with at least two whole periods, then every action at or after `k` occurs
    at least twice, so none of them is globally unique, so the last unique
    action lies before `k` and `novelty_exhausted <= k`. Novelty dominates
    repetition by construction. Measured over the 38 traces in this project's
    labelled data where this fires, it was never once strictly smaller.

    Kept because it measures something real about a run and says it more
    directly than novelty does; it is a diagnostic, not a bound.

    `align._trailing_identical_loop_start` detects the period-1 case only, and
    uses it to discount agreements rather than to locate anything. This
    generalises it to any period: the SWE-agent failures that motivated it
    cycle over blocks of 2 to 14 steps, up to 37 times.
    """
    sigs = [(s.name, repr(sorted(s.args.items()))) for s in steps]
    n = len(sigs)
    best: int | None = None
    for period in range(1, n // 2 + 1):
        matched = 0
        while (
            n - 1 - matched - period >= 0
            and sigs[n - 1 - matched] == sigs[n - 1 - matched - period]
        ):
            matched += 1
        if matched >= period:
            start = n - matched - period
            if start >= 0 and (best is None or start < best):
                best = start
    return None if best is None else best + 1


def novelty_exhausted(steps: Sequence[Step]) -> int:
    """1-based index after which the run does nothing it does not also repeat.

    The last step whose action is globally unique, plus one. Past it every
    action the run takes, it takes at least twice — it has stopped trying
    anything once. Like `terminal_repeat` this is an upper bound on the point of
    no return, and it catches the runs that flail over rotating arguments for a
    long time before locking into an exact cycle.
    """
    sigs = [(s.name, repr(sorted(s.args.items()))) for s in steps]
    counts = Counter(sigs)
    last = 0
    for i, sig in enumerate(sigs, start=1):
        if counts[sig] == 1:
            last = i
    return min(last + 1, len(steps)) if steps else 1


def earliest_bound(bad: Sequence[Step]) -> int:
    """The tightest of the single-trace upper bounds on the point of no return.

    Two independent facts each bound it from above, and neither needs a
    reference run:

      * the first commitment — the run changed something it did not create,
      * novelty exhaustion — the run stops doing anything only once.

    Each says "no later than this", so the earliest is the tightest, and taking
    the minimum is the only thing to do with a set of upper bounds. It is not a
    tuned blend: there is no weight to choose.

    `terminal_repeat` was a third bound here and is not one any more. It cannot
    be the strict minimum: periodicity to the end implies novelty exhaustion no
    later, so novelty dominates it by construction. Dropping it changed no
    prediction on any of the 307 labelled trajectories this project holds, and
    could not have. See its docstring for the argument.

    Measured within ±2 against `first_commitment` alone, over 178 labelled pairs
    from four sets, it gains 8 and loses 2 (McNemar p=0.11) and never loses on
    any individual set: 96/178 against 90/178 pooled. That is a small,
    unseparated improvement; the reason to prefer it is that it costs nothing
    and is bounded by construction.

    It is also the most *stable* rule measured. The withdrawn reference-based
    variant beat it on both OpenHands halves (29/40 and 26/40 against 25/40)
    and lost badly everywhere else (21/58 and 10/40 against 27/58 and 19/40),
    because a reference stops paying off-scaffold. This needs none.
    """
    if not bad:
        raise ValueError("the failure run has no steps to locate a divergence in")
    bounds = [len(bad), novelty_exhausted(bad)]
    commitment = first_commitment(bad)
    if commitment is not None:
        bounds.append(commitment)
    return min(bounds)


Reliability = Literal[
    "commit-short", "silent-short", "commit-long-single",
    "commit-long-many", "silent-long",
]

# Within-±2 accuracy of `earliest_bound` per class, over 313 labelled runs: the
# 178 the classes were defined on, plus 135 scored once afterwards on data none
# of this was fitted to. The ordering is what carries: it held inside all four
# of the original sets separately, and again on the later set, which is what
# makes abstaining on the tail meaningful rather than a guess.
#
# The figures are not precise. Relabelling the same trajectories moved this
# rule's own score by 12 points at ±2, so treat each as a band of roughly that
# width, and the two sparse middle classes as weaker still (27 and 30 runs).
# The first 178 also carry short degenerate trajectories that inflate the two
# short classes, since a trace of five steps or fewer cannot be missed at ±2.
# `contracts/divergence.md` measures both effects.
RELIABILITY_ACCURACY: dict[str, float] = {
    "commit-short": 0.86,        # 68/79
    "silent-short": 0.78,        # 21/27
    "commit-long-single": 0.77,  # 23/30
    "commit-long-many": 0.37,    # 53/142
    "silent-long": 0.20,         # 7/35
}
LONG_TRACE = 18


def reliability(bad: Sequence[Step]) -> Reliability:
    """How much to trust `earliest_bound` on this run, decided without labels.

    Two things predict whether the answer lands: whether the run committed at
    all, and whether the trace is long. `silent-long` — a long run that never
    changed anything pre-existing — is right about a fifth of the time and
    should be treated as "cannot localise" rather than as an answer.

    The 18-step boundary is `align-v1`'s existing long/short split, reused
    rather than refitted.
    """
    commitments = commitment_steps(bad)
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
    return earliest_bound(bad), reliability(bad)
