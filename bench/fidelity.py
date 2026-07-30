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
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from locus import Store, StoredEvent, ToolCallEvent

from .repos import CORPUS_ROOT

STORE = CORPUS_ROOT / "store"
LEDGER = CORPUS_ROOT / "runs.jsonl"
FIDELITY_ROOT = CORPUS_ROOT / "fidelity"
PAIRS = FIDELITY_ROOT / "pairs.jsonl"

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
        out.append(Step(name=event.name, args_hash=event.args_hash))
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
    strict: tuple[bool, ...]
    names: tuple[bool, ...]
    divergence: int | None

    @property
    def compared(self) -> int:
        return len(self.strict)

    @property
    def survived(self) -> int:
        return self.compared if self.divergence is None else self.divergence

    def holds_to(self, k: int) -> bool:
        return self.divergence is None or self.divergence >= k

    def row(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "a": self.a,
            "b": self.b,
            "length_a": self.length_a,
            "length_b": self.length_b,
            "compared": self.compared,
            "strict_matches": sum(self.strict),
            "name_matches": sum(self.names),
            "divergence": self.divergence,
        }


def compare(task_id: str, a: str, b: str, left: list[Step], right: list[Step]) -> Comparison:
    n = min(len(left), len(right))
    strict = tuple(left[i] == right[i] for i in range(n))
    names = tuple(left[i].name == right[i].name for i in range(n))
    divergence = next((i for i, ok in enumerate(strict) if not ok), None)
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
        strict=strict,
        names=names,
        divergence=divergence,
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


def comparisons(
    store: Path = STORE, ledger: Path = LEDGER
) -> tuple[list[Comparison], dict[str, list[Step]]]:
    rows = ledger_rows(ledger)
    db = Store(store)
    try:
        extracted = {
            row["run_id"]: steps(db.events(row["run_id"]), row.get("stop_reason")) for row in rows
        }
    finally:
        db.close()

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
                )
            )
    return (out, extracted)


def chance_agreement(extracted: dict[str, list[Step]]) -> float:
    """What two unrelated runs would agree on by picking from six tool names.

    Without this, a tool-name agreement rate cannot be read at all: the agent
    reads far more often than it does anything else, so two runs sharing a name
    is mostly evidence about the marginal distribution.
    """
    counts = Counter(step.name for run in extracted.values() for step in run)
    total = sum(counts.values())
    if not total:
        return 0.0
    return sum((n / total) ** 2 for n in counts.values())


def _rate_lines(found: list[Comparison], label: str, strict: bool) -> list[str]:
    hits = sum(sum(c.strict if strict else c.names) for c in found)
    total = sum(c.compared for c in found)
    by_task: dict[str, list[float]] = {}
    for c in found:
        if c.compared:
            matches = sum(c.strict if strict else c.names)
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


def _survival_lines(found: list[Comparison]) -> list[str]:
    out = ["step-by-step survival, P(the two runs agree on every step through k):"]
    out.append("      k   pairs with k steps   still agreeing        S(k)          95% CI")
    longest = max((c.compared for c in found), default=0)
    for k in range(1, longest + 1):
        eligible = [c for c in found if c.compared >= k]
        if not eligible:
            continue
        alive = sum(1 for c in eligible if c.holds_to(k))
        lo, hi = wilson(alive, len(eligible))
        bar = "#" * round(alive / len(eligible) * 20)
        out.append(
            f"  {k:>5}   {len(eligible):>18}   {alive:>14}   {bar:<20} "
            f"{alive / len(eligible):>6.1%}  [{lo:.0%}, {hi:.0%}]"
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
        tail = c.strict[c.divergence + 1 :]
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
        hits = sum(sum(c.strict) for c in group)
        total = sum(c.compared for c in group)
        diverged = [c.survived for c in group if c.divergence is not None]
        median = f"{statistics.median(diverged):.0f}" if diverged else "-"
        out.append(
            f"  {name:<16} {len(group):>3} pairs  {hits}/{total} steps  "
            f"{hits / total if total else 0:>6.1%}   median steps agreed {median}"
        )
    return out


def report(store: Path = STORE, ledger: Path = LEDGER, out: Path = PAIRS) -> str:
    found, extracted = comparisons(store, ledger)
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
    lines.append(f"per-pair detail written to {out}")
    return "\n".join(lines)
