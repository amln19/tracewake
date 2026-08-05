"""How far two runs of the same task agree before they stop agreeing.

This measures the agent, not the recorder. Two runs of a corpus task differ in
exactly one input — the sampler seed — so they are two samples from the same
policy on the same prompt, and the rate at which they part company is the noise
floor any later claim about *why* a pair diverged has to beat. A method that
localizes divergence to step three is saying nothing if independent samples
already part at step three on their own.

Matching is strict: same tool name and same normalized arguments. Strict
disagreement counts a rewritten edit body as a divergence even when it means the
same thing, so the number is a lower bound on agreement. Tool-name-only
agreement is reported beside it, and the gap between the two is what a semantic
comparison would have to close.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from locus import Store, StoredEvent, ToolCallEvent

from .repos import CORPUS_ROOT

STORE = CORPUS_ROOT / "store"
LEDGER = CORPUS_ROOT / "runs.jsonl"
FIDELITY_ROOT = CORPUS_ROOT / "fidelity"
PAIRS = FIDELITY_ROOT / "pairs.jsonl"
# Fresh recordings for the replay-fidelity arm. Separate from the closed corpus
# so measuring the tool never writes into runs reserved for alignment.
REPLAY_STORE = FIDELITY_ROOT / "replay-store"
REPLAY_LEDGER = FIDELITY_ROOT / "replay-runs.jsonl"
REPLAY_RESULTS = FIDELITY_ROOT / "replay-results.jsonl"
REPLAY_ARM_SEED = 20260730
REPLAY_ARM_N = 8

# A recorded run seeds the sampler from `run_index * RUN_STRIDE`, then advances by
# one per model call, so each run owns the window `[seed, seed + RUN_STRIDE)`. Any
# later arm that lands inside an occupied window re-seeds the sampler exactly as
# the recording did and reproduces it token for token — reporting perfect
# agreement while measuring nothing but the RNG. `assert_fresh` is what stops
# that, and it is the one check this module cannot do without.
RUN_STRIDE = 1013
FRESH_BASE = 7_000_003


def recorded_seed(run_index: int) -> int:
    return run_index * RUN_STRIDE


def fresh_seed(run_index: int, replicate: int = 0) -> int:
    return FRESH_BASE + (replicate * 64 + run_index) * RUN_STRIDE


def assert_fresh(seed: int, task_id: str, recorded: Iterable[int]) -> None:
    for other in recorded:
        if abs(seed - other) < RUN_STRIDE:
            raise ValueError(
                f"seed {seed} for {task_id} lands within {RUN_STRIDE} of recorded seed "
                f"{other}. The sampler would be re-seeded as the recording seeded it and "
                f"the run would reproduce it, so the comparison would report agreement it "
                f"never observed. Pick a seed at least {RUN_STRIDE} from every recorded "
                f"seed — `fresh_seed()` does."
            )


@dataclass(frozen=True)
class Step:
    name: str
    args_hash: str
    # What the action was aimed at, with the rest of the arguments dropped: the
    # file for a read or an edit, the pattern for a search. Two runs reading the
    # same file at different offsets, or editing the same file with different
    # text, agree at this granularity and not at the strict one — which is the
    # difference between comparing steps by equality and scoring them by
    # similarity.
    target: str = ""


def strict_key(step: Step) -> tuple[str, str]:
    return (step.name, step.args_hash)


def target_key(step: Step) -> tuple[str, str]:
    return (step.name, step.target)


def name_key(step: Step) -> tuple[str, str]:
    return (step.name, "")


GRANULARITIES = (
    ("name and all args", strict_key),
    ("name and target", target_key),
    ("name only", name_key),
)


def target_of(args: dict) -> str:
    for field in ("path", "query"):
        if field in args:
            return str(args[field])
    return ""


# A submitted run ends by claiming it is done, which never reaches the tool
# dispatcher and so leaves no event. One run submitting where the other kept
# working is a real parting of ways, so the claim is appended as a terminal step
# from the ledger rather than dropped.
SUBMIT = Step(name="submit", args_hash="")


def steps(events: Sequence[StoredEvent], stop_reason: str | None = None) -> list[Step]:
    out: list[Step] = []
    for stored in events:
        event = stored.event
        if not isinstance(event, ToolCallEvent):
            continue
        # Insertion order, deliberately not `canonical_order`: that sorts by
        # `tool_call_id`, which is a string, so step10 would land before step2.
        # The index is parsed back out and checked so a change in either the id
        # format or the sequential-dispatch assumption fails here rather than
        # quietly shifting every comparison by one.
        index = int(event.tool_call_id.split("-", 1)[0].removeprefix("step"))
        if index != len(out):
            raise ValueError(
                f"tool call {event.tool_call_id!r} is at position {len(out)} of its run but "
                f"names step {index}. Step order can no longer be read off insertion order; "
                f"fix the extraction before trusting any comparison."
            )
        out.append(
            Step(
                name=event.name,
                args_hash=event.args_hash,
                target=target_of(event.args),
            )
        )
    if stop_reason == "submitted":
        out.append(SUBMIT)
    return out


@dataclass(frozen=True)
class Comparison:
    task_id: str
    a: str
    b: str
    length_a: int
    length_b: int
    # Agreement at whatever granularity this comparison was built with, position
    # by position. `names` is always the loosest reading, for the gap between them.
    agree: tuple[bool, ...]
    names: tuple[bool, ...]
    divergence: int | None
    coverage: tuple[bool, bool] = (False, False)
    resolve: tuple[bool, bool] = (False, False)

    @property
    def compared(self) -> int:
        return len(self.agree)

    @property
    def survived(self) -> int:
        return self.compared if self.divergence is None else self.divergence

    @property
    def mixed(self) -> bool:
        return self.coverage[0] != self.coverage[1]

    @property
    def length_ratio(self) -> float:
        shorter = min(self.length_a, self.length_b)
        return max(self.length_a, self.length_b) / shorter if shorter else float("inf")

    def holds_to(self, k: int) -> bool:
        return self.divergence is None or self.divergence >= k

    @property
    def realigns(self) -> bool:
        """Whether the runs ever agree again after their first difference.

        When they do not, the last position after which two runs never re-align
        *is* the position where they first differ, so a backward divergence
        definition and a forward one pick the same step and cannot be told apart
        by any evaluation.
        """
        if self.divergence is None or self.divergence >= self.compared:
            return False
        return any(self.agree[self.divergence + 1 :])

    def row(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "a": self.a,
            "b": self.b,
            "length_a": self.length_a,
            "length_b": self.length_b,
            "compared": self.compared,
            "strict_matches": sum(self.agree),
            "name_matches": sum(self.names),
            "divergence": self.divergence,
            "mixed": self.mixed,
            "length_ratio": round(self.length_ratio, 2),
            "realigns": self.realigns,
        }


def compare(
    task_id: str,
    a: str,
    b: str,
    left: list[Step],
    right: list[Step],
    coverage: tuple[bool, bool] = (False, False),
    resolve: tuple[bool, bool] = (False, False),
    key: Callable[[Step], tuple[str, str]] = strict_key,
) -> Comparison:
    n = min(len(left), len(right))
    agree = tuple(key(left[i]) == key(right[i]) for i in range(n))
    names = tuple(left[i].name == right[i].name for i in range(n))
    divergence = next((i for i, ok in enumerate(agree) if not ok), None)
    # Agreeing on every step both runs took, where one then took more, is still a
    # parting: the shorter run stopped where the longer one carried on.
    if divergence is None and len(left) != len(right):
        divergence = n
    return Comparison(
        task_id=task_id,
        a=a,
        b=b,
        length_a=len(left),
        length_b=len(right),
        agree=agree,
        names=names,
        divergence=divergence,
        coverage=coverage,
        resolve=resolve,
    )


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def clustered(values: Sequence[float], z: float = 1.96) -> tuple[float, float, float]:
    """Mean over independent units, with an interval that respects the clustering.

    The unit here is the task, not the pair: three runs of one task give three
    pairs that share runs, so pooling them as independent would understate the
    interval.
    """
    if not values:
        return (0.0, 0.0, 0.0)
    mean = statistics.fmean(values)
    if len(values) < 2:
        return (mean, mean, mean)
    error = statistics.stdev(values) / math.sqrt(len(values))
    return (mean, max(0.0, mean - z * error), min(1.0, mean + z * error))


def operator_of(task_id: str) -> str:
    parts = task_id.split("-")
    return parts[1] if len(parts) > 2 else "unknown"


def length_bucket(comparison: Comparison) -> str:
    shorter = min(comparison.length_a, comparison.length_b)
    if shorter <= 5:
        return "2-5 steps"
    if shorter <= 15:
        return "6-15 steps"
    return "16+ steps"


def ledger_rows(ledger: Path = LEDGER) -> list[dict]:
    if not ledger.exists():
        raise FileNotFoundError(
            f"no attempt ledger at {ledger}. The corpus has to exist before its "
            f"run-to-run divergence can be measured."
        )
    return [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def extract(
    store: Path = STORE, ledger: Path = LEDGER
) -> tuple[list[dict], dict[str, list[Step]]]:
    rows = ledger_rows(ledger)
    extracted: dict[str, list[Step]] = {}
    stored_any = False
    db = Store(store)
    try:
        for row in rows:
            events = db.events(row["run_id"])
            stored_any = stored_any or bool(events)
            # Every field is required rather than defaulted. A ledger missing
            # `coverage` would silently make every pair same-outcome and report
            # a clean result for the comparison that matters most.
            extracted[row["run_id"]] = steps(events, row["stop_reason"])
    finally:
        db.close()
    # The ledger ships but the store it indexes does not, and opening a Store
    # creates an empty one rather than failing. Checked on events rather than on
    # the extracted steps, because a submitted run yields a terminal step even
    # when nothing was recorded — so a missing corpus would otherwise read as
    # one where every run submitted immediately, and score clean.
    if rows and not stored_any:
        raise FileNotFoundError(
            f"the ledger at {ledger} lists {len(rows)} runs but the store at {store} holds "
            f"events for none of them. The recorded runs are not committed; rebuild them "
            f"with `python -m bench run`, or point the store at one that has them."
        )
    return (rows, extracted)


def pair_up(
    rows: Sequence[dict],
    extracted: dict[str, list[Step]],
    key: Callable[[Step], tuple[str, str]] = strict_key,
) -> list[Comparison]:
    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)

    out: list[Comparison] = []
    for task_id, group in sorted(by_task.items()):
        ordered = sorted(group, key=lambda r: r["run_index"])
        for left, right in combinations(ordered, 2):
            out.append(
                compare(
                    task_id,
                    left["run_id"],
                    right["run_id"],
                    extracted[left["run_id"]],
                    extracted[right["run_id"]],
                    coverage=(bool(left["coverage"]), bool(right["coverage"])),
                    resolve=(bool(left["resolve"]), bool(right["resolve"])),
                    key=key,
                )
            )
    return out


def comparisons(
    store: Path = STORE, ledger: Path = LEDGER
) -> tuple[list[Comparison], dict[str, list[Step]]]:
    rows, extracted = extract(store, ledger)
    return (pair_up(rows, extracted), extracted)


def chance_agreement(
    extracted: dict[str, list[Step]],
    key: Callable[[Step], tuple[str, str]] = name_key,
) -> float:
    """What two unrelated runs would agree on by coincidence at this width.

    Without this, an agreement rate cannot be read at all: the agent reads far
    more often than it does anything else, so two runs sharing a tool name is
    mostly evidence about the marginal distribution. It matters most for the
    widest comparison and least for the narrowest.
    """
    counts = Counter(key(step) for run in extracted.values() for step in run)
    total = sum(counts.values())
    if not total:
        return 0.0
    return sum((n / total) ** 2 for n in counts.values())


def _rate_lines(found: list[Comparison], label: str, strict: bool) -> list[str]:
    hits = sum(sum(c.agree if strict else c.names) for c in found)
    total = sum(c.compared for c in found)
    by_task: dict[str, list[float]] = {}
    for c in found:
        if c.compared:
            matches = sum(c.agree if strict else c.names)
            by_task.setdefault(c.task_id, []).append(matches / c.compared)
    per_task = [statistics.fmean(v) for v in by_task.values()]
    mean, lo, hi = clustered(per_task)
    pooled_lo, pooled_hi = wilson(hits, total)
    return [
        label,
        (
            f"  pooled over positions   {hits / total:.1%}  ({hits}/{total})  "
            f"[{pooled_lo:.1%}, {pooled_hi:.1%}]"
        ),
        f"  mean over {len(per_task)} tasks       {mean:.1%}  [{lo:.1%}, {hi:.1%}]",
    ]


def survival_at(found: Sequence[Comparison], k: int) -> tuple[int, int]:
    eligible = [c for c in found if c.compared >= k]
    return (sum(1 for c in eligible if c.holds_to(k)), len(eligible))


def _survival_lines(found: list[Comparison]) -> list[str]:
    out = ["step-by-step survival, P(the two runs agree on every step through k):"]
    out.append("      k   pairs with k steps   still agreeing        S(k)          95% CI")
    longest = max((c.compared for c in found), default=0)
    for k in range(1, longest + 1):
        alive, eligible = survival_at(found, k)
        if not eligible:
            continue
        lo, hi = wilson(alive, eligible)
        bar = "#" * round(alive / eligible * 20)
        out.append(
            f"  {k:>5}   {eligible:>18}   {alive:>14}   {bar:<20} "
            f"{alive / eligible:>6.1%}  [{lo:.0%}, {hi:.0%}]"
        )
    return out


def _survival_table(groups: dict[str, list[Comparison]], depth: int = 8) -> list[str]:
    names = list(groups)
    header = "      k  " + "".join(f"{name:>22}" for name in names)
    out = [header, "         " + "".join(f"{'n     S(k)':>22}" for _ in names)]
    for k in range(1, depth + 1):
        cells = []
        for name in names:
            alive, eligible = survival_at(groups[name], k)
            cells.append(f"{eligible:>3}  {alive / eligible:>7.1%}" if eligible else f"{'-':>12}")
        out.append(f"  {k:>5}  " + "".join(f"{cell:>22}" for cell in cells))
    return out


def _definition_lines(groups: dict[str, list[Comparison]]) -> list[str]:
    """Whether a backward divergence definition can differ from a forward one.

    The project's divergence point is the last position after which two runs never
    re-align. When a pair never re-aligns at all, that position is the one where
    they first differed — the same answer the simplest baseline gives. If that is
    most pairs, an aligner and the baseline are scored on the same targets and
    neither can beat the other by much.
    """
    out = [
        "pairs that ever agree again after first differing",
        "(where they never do, the backward divergence definition and the",
        " first-difference baseline pick the same step):",
    ]
    for name, group in groups.items():
        parted = [c for c in group if c.divergence is not None]
        if not parted:
            out.append(f"  {name:<22} no pair parted")
            continue
        again = sum(1 for c in parted if c.realigns)
        lo, hi = wilson(again, len(parted))
        out.append(
            f"  {name:<22} {again}/{len(parted)} ({again / len(parted):>5.1%}) "
            f"[{lo:.0%}, {hi:.0%}]  so the two definitions coincide on "
            f"{(len(parted) - again) / len(parted):.0%}"
        )
    return out


def _parting_lines(found: list[Comparison]) -> list[str]:
    counts: dict[int, int] = {}
    for c in found:
        if c.divergence is not None:
            counts[c.divergence] = counts.get(c.divergence, 0) + 1
    out = ["which action the two runs first differ on:"]
    for step in sorted(counts):
        out.append(f"  action {step + 1:<3} {'#' * counts[step]:<40} {counts[step]}")
    return out


def _recovery_lines(found: list[Comparison], chance: float) -> list[str]:
    """How often the runs agree again after their first disagreement.

    This is the measurement that decides whether the first difference is a useful
    definition of where a pair diverged. If two runs differ once and then go back
    to agreeing, the first difference is noise, and the position after which they
    never agree again is the thing worth locating.
    """
    strict_hits = strict_total = name_hits = name_total = 0
    recovered = 0
    for c in found:
        if c.divergence is None or c.divergence >= c.compared:
            continue
        tail = c.agree[c.divergence + 1 :]
        names = c.names[c.divergence + 1 :]
        strict_hits += sum(tail)
        strict_total += len(tail)
        name_hits += sum(names)
        name_total += len(names)
        recovered += any(tail)
    if not strict_total:
        return ["no pair had a step left after its first difference"]
    return [
        "after the first difference:",
        (
            f"  positions still matching strictly   {strict_hits}/{strict_total} "
            f"({strict_hits / strict_total:.1%})"
        ),
        (
            f"  positions still matching on name    {name_hits}/{name_total} "
            f"({name_hits / name_total:.1%}, chance is {chance:.1%})"
        ),
        f"  pairs that agree strictly on some later step {recovered}",
    ]


def _stratum_lines(found: list[Comparison], label: str, key) -> list[str]:
    groups: dict[str, list[Comparison]] = {}
    for c in found:
        groups.setdefault(key(c), []).append(c)
    out = [f"strict agreement by {label}:"]
    for name in sorted(groups):
        group = groups[name]
        hits = sum(sum(c.agree) for c in group)
        total = sum(c.compared for c in group)
        diverged = [c.survived for c in group if c.divergence is not None]
        median = f"{statistics.median(diverged):.0f}" if diverged else "-"
        out.append(
            f"  {name:<16} {len(group):>3} pairs  {hits}/{total} steps  "
            f"{hits / total if total else 0:>6.1%}   median steps agreed {median}"
        )
    return out


def _granularity_lines(rows: Sequence[dict], extracted: dict[str, list[Step]]) -> list[str]:
    """The same pairs compared by equality at three widths.

    The distance function alignment will use scores argument *similarity* rather
    than testing equality, so strict agreement is the most pessimistic reading
    available. Widening the unit is the cheapest way to find out whether there is
    any shared prefix for an aligner to work with, or whether the runs part on
    which file to touch and not merely on how to touch it.
    """
    out = [
        "coverage-mixed pairs compared at three widths (S(k), pairs still agreeing):",
        "      k  " + "".join(f"{label:>22}" for label, _ in GRANULARITIES),
    ]
    graded = {
        label: [c for c in pair_up(rows, extracted, key) if c.mixed]
        for label, key in GRANULARITIES
    }
    for k in range(1, 7):
        cells = []
        for label, _ in GRANULARITIES:
            alive, eligible = survival_at(graded[label], k)
            cells.append(f"{eligible:>3}  {alive / eligible:>7.1%}" if eligible else f"{'-':>12}")
        out.append(f"  {k:>5}  " + "".join(f"{cell:>22}" for cell in cells))
    out.append("")
    for label, key in GRANULARITIES:
        group = graded[label]
        parted = [c for c in group if c.divergence is not None]
        again = sum(1 for c in parted if c.realigns)
        out.append(
            f"  {label:<20} {again}/{len(parted)} pairs agree again after first differing "
            f"({again / len(parted):>5.1%}), coincidence rate "
            f"{chance_agreement(extracted, key):.1%}"
        )
    return out


def report(store: Path = STORE, ledger: Path = LEDGER, out: Path = PAIRS) -> str:
    rows, extracted = extract(store, ledger)
    found = pair_up(rows, extracted)
    if not found:
        return f"no comparable pairs in {ledger}"
    chance = chance_agreement(extracted)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for c in found:
            fh.write(json.dumps(c.row(), sort_keys=True) + "\n")

    tasks = {c.task_id for c in found}
    identical = [c for c in found if c.divergence is None]
    immediate = [c for c in found if c.divergence == 0]
    diverged = [c.survived for c in found if c.divergence is not None]
    lines = [
        "run-to-run divergence: two samples of one task, differing only in sampler seed",
        "",
        f"pairs               {len(found)} over {len(tasks)} tasks",
        f"identical outright  {len(identical)}/{len(found)} ({len(identical) / len(found):.1%})",
        (
            f"differ on action 1  {len(immediate)}/{len(found)} "
            f"({len(immediate) / len(found):.1%})"
        ),
        (
            f"steps agreed before parting  median {statistics.median(diverged):.0f}, "
            f"mean {statistics.fmean(diverged):.1f}"
        )
        if diverged
        else "steps agreed before parting  every pair agreed throughout",
        "",
    ]
    lines += _rate_lines(found, "strict agreement (tool name and normalized args):", strict=True)
    lines.append("")
    lines += _rate_lines(found, f"tool name only (chance is {chance:.1%}):", strict=False)
    lines.append("")
    lines += _survival_lines(found)
    lines.append("")
    lines += _parting_lines(found)
    lines.append("")
    lines += _recovery_lines(found, chance)
    lines.append("")
    lines += _stratum_lines(found, "trajectory length", length_bucket)
    lines.append("")
    lines += _stratum_lines(found, "operator", lambda c: operator_of(c.task_id))
    lines.append("")

    # The pairs alignment will actually be evaluated on are not arbitrary repeats:
    # one run produced a patch and the other did not. If they part as early as any
    # other pair, the divergence to be localized is at step two and there is
    # nothing for an aligner to find that a first-difference check would miss.
    groups = {
        "all pairs": found,
        "coverage-mixed": [c for c in found if c.mixed],
        "same outcome": [c for c in found if not c.mixed],
        "mixed, under 4:1": [c for c in found if c.mixed and c.length_ratio <= 4],
    }
    lines.append("survival by pair type — the alignment pairs against everything else:")
    lines += _survival_table(groups)
    lines.append("")
    lines += _definition_lines(groups)
    lines.append("")
    lines += _granularity_lines(rows, extracted)
    lines.append("")
    mixed_tasks = {c.task_id for c in groups["coverage-mixed"]}
    lines.append(
        f"coverage-mixed pairs: {len(groups['coverage-mixed'])} over {len(mixed_tasks)} tasks; "
        f"{len(groups['mixed, under 4:1'])} of them inside the 4:1 length cap"
    )
    lines.append(f"per-pair detail written to {out}")
    return "\n".join(lines)


# --- Replay fidelity ----------------------------------------------------------
# A claim about the tool: a cassette replayed through `locus.replay` with the
# network blocked reproduces its run. Fresh recordings, not the closed corpus —
# those predate the current agent, and this arm only needs *a* recording.


def pick_replay_tasks(n: int = REPLAY_ARM_N, seed: int = REPLAY_ARM_SEED) -> list:
    from .tasks import load

    tasks = list(load())
    random.Random(seed).shuffle(tasks)
    return tasks[:n]


def record_replay_arm(
    n: int = REPLAY_ARM_N,
    store: Path = REPLAY_STORE,
    ledger: Path = REPLAY_LEDGER,
    max_steps: int = 18,
) -> None:
    """Record `n` fresh runs with the current agent into a store of their own."""
    from .backend import DEFAULT_MODEL, LocalModel
    from .runner import attempt, done, record_attempt

    chosen = pick_replay_tasks(n)
    # Seed far from the corpus windows so these recordings are never mistaken
    # for a same-seed control against corpus runs.
    model = LocalModel(model_id=DEFAULT_MODEL, temperature=0.7, seed=FRESH_BASE)
    model.warm()
    finished = done(ledger)
    print(
        f"replay-fidelity record: {len(chosen)} tasks, "
        f"{len(finished)} already in {ledger}",
        flush=True,
    )
    for position, task in enumerate(chosen, start=1):
        key = f"{task.task_id}#0"
        if key in finished:
            print(f"[{position}/{len(chosen)}] {key} already recorded", flush=True)
            continue
        # run_index 0 keeps the name simple; the LocalModel seed is already fresh.
        result = attempt(task, 0, model, store=store, max_steps=max_steps)
        record_attempt(result, ledger)
        print(
            f"[{position}/{len(chosen)}] {result.key:<34} "
            f"coverage={int(result.coverage)} resolve={int(result.resolve)} "
            f"turns={result.turns:<3} actions={result.actions:<3} "
            f"{result.stop_reason:<12} {result.seconds}s  run={result.run_id[:8]}",
            flush=True,
        )


def _forbidden_model(*args: object, **kwargs: object):
    raise AssertionError(
        "replay reached the live model. Replay fidelity requires every completion "
        "to come from the cassette."
    )


def replay_one(task_id: str, run_id: str, store: Path = REPLAY_STORE) -> dict:
    """Replay one recording with the network blocked; model calls from the log."""
    import shutil
    import tempfile
    import time

    import locus
    from locus import ReplayMiss

    from . import agent
    from .repos import BY_NAME
    from .runner import prepare
    from .tasks import load

    from .backend import DEFAULT_MODEL, PROVIDER

    task = next(t for t in load() if t.task_id == task_id)
    repo = BY_NAME[task.repo]
    scratch = Path(tempfile.mkdtemp(prefix=f"replay-{task_id}-"))
    root = prepare(task, scratch / "repo")
    started = time.time()
    outcome = {
        "task_id": task_id,
        "run_id": run_id,
        "ok": False,
        "error": None,
        "matched": 0,
        "missed": 0,
        "degraded": 0,
        "tool_calls_replayed": 0,
        "seconds": 0.0,
    }
    try:
        with locus.replay(run_id, store=store, block_network=True) as session:
            tools = agent.Tools(
                session,
                root,
                repo.source_dirs,
                lambda: (_ for _ in ()).throw(
                    AssertionError("replay reached the live test runner")
                ),
            )
            # model_id must match the recording — matching includes it.
            model = session.model(
                provider=PROVIDER,
                model_id=DEFAULT_MODEL,
                stream_fn=_forbidden_model,
                create_fn=_forbidden_model,
            )
            agent.run(session, model, task.issue, tools, max_steps=18, temperature=0.7)
            report = session.report
            outcome.update(
                ok=report.missed == 0 and report.matched > 0,
                matched=report.matched,
                missed=report.missed,
                degraded=report.degraded,
                tool_calls_replayed=report.tool_calls_replayed,
            )
    except ReplayMiss as exc:
        outcome["error"] = str(exc)
        outcome["ok"] = False
    except AssertionError as exc:
        outcome["error"] = str(exc)
        outcome["ok"] = False
    finally:
        outcome["seconds"] = round(time.time() - started, 1)
        shutil.rmtree(scratch, ignore_errors=True)
    return outcome


def measure_replay_arm(
    store: Path = REPLAY_STORE,
    ledger: Path = REPLAY_LEDGER,
    results: Path = REPLAY_RESULTS,
) -> str:
    """Replay every fresh recording and write the per-run result sheet."""
    if not ledger.exists():
        raise FileNotFoundError(
            f"no replay-fidelity ledger at {ledger}. Record the arm first with "
            f"`python -m bench replay-fidelity record`."
        )
    rows = [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    out_rows = []
    results.parent.mkdir(parents=True, exist_ok=True)
    print(f"replay-fidelity measure: {len(rows)} recordings in {store}", flush=True)
    for position, row in enumerate(rows, start=1):
        result = replay_one(row["task_id"], row["run_id"], store=store)
        out_rows.append(result)
        flag = "ok" if result["ok"] else "FAIL"
        print(
            f"[{position}/{len(rows)}] {row['task_id']:<34} {flag}  "
            f"matched={result['matched']} missed={result['missed']} "
            f"tools={result['tool_calls_replayed']}  {result['seconds']}s"
            + (f"  {result['error'][:80]}" if result["error"] else ""),
            flush=True,
        )
    with results.open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return replay_report(results)


def replay_report(results: Path = REPLAY_RESULTS) -> str:
    if not results.exists():
        return f"no replay-fidelity results at {results}"
    rows = [
        json.loads(line)
        for line in results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        return f"no replay-fidelity results at {results}"
    ok = sum(1 for r in rows if r["ok"])
    matched = sum(r["matched"] for r in rows)
    missed = sum(r["missed"] for r in rows)
    tools = sum(r["tool_calls_replayed"] for r in rows)
    lo, hi = wilson(ok, len(rows))
    lines = [
        "replay fidelity: cassette replayed through locus.replay, network blocked",
        "",
        f"recordings reproduced  {ok}/{len(rows)} ({ok / len(rows):.1%})  [{lo:.0%}, {hi:.0%}]",
        f"model calls matched    {matched}  missed {missed}  degraded "
        f"{sum(r['degraded'] for r in rows)}",
        f"tool calls replayed    {tools}",
        f"wall clock             {sum(r['seconds'] for r in rows):.0f}s",
        "",
    ]
    for r in rows:
        flag = "ok" if r["ok"] else "FAIL"
        lines.append(
            f"  {r['task_id']:<34} {flag}  matched={r['matched']} missed={r['missed']}"
        )
        if r["error"]:
            lines.append(f"    {r['error'][:120]}")
    return "\n".join(lines)


def fidelity_gate() -> str:
    """Both fidelity numbers: run-to-run divergence, then replay fidelity."""
    parts = [
        "=" * 72,
        "FIDELITY GATE",
        "=" * 72,
        "",
        report(),
        "",
        "=" * 72,
        "",
        replay_report(),
    ]
    return "\n".join(parts)
