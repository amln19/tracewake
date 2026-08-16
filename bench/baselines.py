"""The superseded divergence rules, kept as evaluation baselines.

`earliest_bound` was what `tracewake.diverge.localize` reported until an
independent rebuild replaced it. It is not product code any more and nothing in
`tracewake/` imports it. It lives here because every published figure in
`contracts/divergence.md` is its, and those figures have to stay reproducible:
the rule that replaced it cannot be scored on the same data, since 107 of the
178 pairs `bench.pooled` reports are its development set.

So these are baselines in the evaluation harness, in the same sense as
`align-v1` and the fitted constant already were — the things a new rule is
measured against, not things the tool runs.

`commitment_steps` stays in `tracewake.diverge` rather than moving here:
`reliability` needs it, and it is the shared definition of what a commitment is.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from tracewake.align import Step
from tracewake.diverge import commitment_steps


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
