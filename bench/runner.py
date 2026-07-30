"""Running the corpus: one task attempt at a time, recorded and labelled.

Each attempt gets a private copy of the repository with the bug already in it, so
runs cannot contaminate each other, and the copy is thrown away once the outcome
is recorded — the log holds everything the run consumed.
"""

from __future__ import annotations

import difflib
import json
import random
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import locus
from locus import Store, Usage

from . import agent, repos
from .backend import DEFAULT_MODEL, PROVIDER, LocalModel
from .repos import CORPUS_ROOT, BY_NAME, SuiteReport, working_copy
from .tasks import Task, apply_mutation, load, relative_source_files

ORDER_SEED = 20260729

STORE = CORPUS_ROOT / "store"
LEDGER = CORPUS_ROOT / "runs.jsonl"


@dataclass(frozen=True)
class Attempt:
    task_id: str
    run_index: int
    run_id: str
    coverage: bool
    resolve: bool
    # Turns and actions come apart when the model replies without a usable
    # action. A run with many turns and few actions is stuck, not hard, and
    # nothing else in the record distinguishes the two.
    turns: int
    actions: int
    edits: int
    repeats: int
    parse_failures: int
    stop_reason: str
    seconds: float
    summary: str

    @property
    def key(self) -> str:
        return f"{self.task_id}#{self.run_index}"


def prepare(task: Task, destination: Path) -> Path:
    """A working copy with the injected bug in place."""
    repo = BY_NAME[task.repo]
    root = working_copy(repo, destination)
    target = root / task.ground_truth_file
    target.write_text(
        apply_mutation(target.read_text(encoding="utf-8"), task.mutation), encoding="utf-8"
    )
    return root


def _text(path: Path) -> str:
    """File contents, or empty for a file that is not there.

    A file can be missing from either side: the working copy may hold something
    the pinned checkout does not, and grading must not die on it — this runs at
    the end of every attempt in a job that takes hours.
    """
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def patch_of(task: Task, root: Path) -> str:
    """A unified diff from the broken state to whatever the agent left behind."""
    repo = BY_NAME[task.repo]
    chunks: list[str] = []
    seen = relative_source_files(root, repo.source_dirs)
    for relative in dict.fromkeys(seen + relative_source_files(repo.path, repo.source_dirs)):
        current = _text(root / relative)
        original = _text(repo.path / relative)
        if relative == task.ground_truth_file:
            original = apply_mutation(original, task.mutation)
        if current == original:
            continue
        chunks.extend(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    return "".join(chunks)


def grade(task: Task, root: Path, report: SuiteReport) -> tuple[bool, bool, str]:
    """Coverage and resolve for one finished attempt.

    Coverage is the primary label: did the run leave behind a well-formed,
    applicable patch — source that still parses, actually differs from the broken
    state, and touches library code rather than tests. Resolve is the stricter
    question of whether that patch made the suite green again. Both are recorded
    because a weak model produces patches far more often than it produces fixes,
    so resolve alone would give a corpus with almost no positive examples.
    """
    patch = patch_of(task, root)
    if not patch.strip():
        return (False, False, patch)
    for relative in {
        line[6:].strip() for line in patch.splitlines() if line.startswith("+++ b/")
    }:
        if "test" in Path(relative).name:
            return (False, False, patch)
        try:
            compile(_text(root / relative), relative, "exec")
        except SyntaxError:
            return (False, False, patch)
    return (True, report.green, patch)


def attempt(
    task: Task,
    run_index: int,
    model: LocalModel,
    store: Path = STORE,
    max_steps: int = 18,
) -> Attempt:
    repo = BY_NAME[task.repo]
    scratch = Path(tempfile.mkdtemp(prefix=f"bench-{task.task_id}-"))
    root = prepare(task, scratch / "repo")
    started = time.time()

    def run_suite() -> tuple[str, bool]:
        report = repos.run_tests(repo, root, timeout=max(60.0, repo.baseline_seconds * 20))
        return (report.output or report.summary, report.green)

    try:
        with locus.record(
            f"{task.task_id}#{run_index}",
            store=store,
            task_id=task.task_id,
            block_network=False,
        ) as session:
            tools = agent.Tools(session, root, repo.source_dirs, run_suite)
            backend = replace(model, seed=model.seed + run_index * 1013, calls=0)
            handle = session.model(
                provider=PROVIDER, model_id=backend.model_id, stream_fn=backend.stream
            )
            trace = agent.run(
                session,
                handle,
                task.issue,
                tools,
                max_steps=max_steps,
                temperature=model.temperature,
            )
            final = repos.run_tests(repo, root, timeout=max(60.0, repo.baseline_seconds * 20))
            coverage, resolve, patch = grade(task, root, final)
            session.outcome(
                status="ok",
                usage=trace.usage,
                coverage=coverage,
                resolve=resolve,
                patch=patch or None,
                test_summary=final.summary,
            )
            run_id = session.run_id
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return Attempt(
        task_id=task.task_id,
        run_index=run_index,
        run_id=run_id,
        coverage=coverage,
        resolve=resolve,
        turns=trace.turns,
        actions=trace.actions_taken,
        edits=trace.edits,
        repeats=trace.repeats,
        parse_failures=trace.parse_failures,
        stop_reason=trace.stop_reason,
        seconds=round(time.time() - started, 1),
        summary=final.summary,
    )


def done(ledger: Path = LEDGER) -> set[str]:
    if not ledger.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def record_attempt(result: Attempt, ledger: Path = LEDGER) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {"key": result.key, **result.__dict__}
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()


def batch(
    runs: int = 5,
    limit: int | None = None,
    max_steps: int = 18,
    model_id: str = DEFAULT_MODEL,
    temperature: float = 0.7,
    store: Path = STORE,
    ledger: Path = LEDGER,
    shard: int = 0,
    shards: int = 1,
) -> None:
    """Run every task `runs` times, skipping whatever the ledger already holds.

    Appending each attempt as it finishes is what makes this restartable: the job
    runs for hours, and losing it to a crash near the end would mean starting
    over. `shard` splits the work across concurrent workers; they share the store
    and the ledger, both of which take concurrent appends, and the ledger check
    makes an overlap harmless rather than a duplicate.
    """
    tasks = load()[:limit]
    model = LocalModel(model_id=model_id, temperature=temperature)
    model.warm()
    finished = done(ledger)
    # Tasks are shuffled with a fixed seed, but a task's runs stay together. Both
    # halves matter for a job that gets read long before it ends: shuffling makes
    # any prefix a sample across all sixteen repositories, and keeping runs
    # together means tasks *complete* as the job goes, so the successes-per-task
    # histogram — the thing the gate turns on — has data early instead of only at
    # the very end.
    order = list(tasks)
    random.Random(ORDER_SEED).shuffle(order)
    planned = [(t, i) for t in order for i in range(runs)]
    mine = [pair for position, pair in enumerate(planned) if position % shards == shard]
    remaining = [(t, i) for t, i in mine if f"{t.task_id}#{i}" not in finished]
    label = f"shard {shard + 1}/{shards} " if shards > 1 else ""
    print(
        f"{label}{len(tasks)} tasks x {runs} runs = {len(planned)} attempts, "
        f"{len(mine)} in this shard, {len(finished)} already done, "
        f"{len(remaining)} to go",
        flush=True,
    )

    for position, (task, index) in enumerate(remaining, start=1):
        result = attempt(task, index, model, store=store, max_steps=max_steps)
        record_attempt(result, ledger)
        print(
            f"[{label}{position}/{len(remaining)}] {result.key:<34} "
            f"coverage={int(result.coverage)} resolve={int(result.resolve)} "
            f"turns={result.turns:<3} actions={result.actions:<3} "
            f"edits={result.edits} repeats={result.repeats:<3} "
            f"{result.stop_reason:<12} {result.seconds}s",
            flush=True,
        )


def status(ledger: Path = LEDGER) -> str:
    if not ledger.exists():
        return f"no attempts yet at {ledger}"
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        return f"no attempts yet at {ledger}"

    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)

    coverage = sum(r["coverage"] for r in rows)
    resolve = sum(r["resolve"] for r in rows)
    mixed = [t for t, rs in by_task.items() if 0 < sum(r["coverage"] for r in rs) < len(rs)]
    mixed_resolve = [
        t for t, rs in by_task.items() if 0 < sum(r["resolve"] for r in rs) < len(rs)
    ]
    seconds = sum(r["seconds"] for r in rows)
    lines = [
        f"attempts           {len(rows)} over {len(by_task)} tasks",
        f"coverage rate      {coverage / len(rows):.1%}  ({coverage}/{len(rows)})",
        f"resolve rate       {resolve / len(rows):.1%}  ({resolve}/{len(rows)})",
        f"mixed on coverage  {len(mixed)} tasks",
        f"mixed on resolve   {len(mixed_resolve)} tasks",
        f"wall clock         {seconds / 3600:.2f} h  ({seconds / len(rows):.0f}s per run)",
    ]
    lines.append("")
    lines.append(_by_operator(rows))
    lines.append("")
    lines.append(_trajectories(rows))
    # Read the run count off the data rather than assuming it. A batch run with
    # `--runs 3` against a hardcoded 5 reported that no task had finished and
    # printed no histogram at all, which is the one number the gate turns on.
    runs = Counter(len(rs) for rs in by_task.values()).most_common(1)[0][0]
    for label in ("coverage", "resolve"):
        lines.append("")
        lines.append(f"successes per task, {label} (tasks with all {runs} runs in):")
        lines.append(_histogram(by_task, label, runs))
    return "\n".join(lines)


def _by_operator(rows: list[dict]) -> str:
    """Outcome rate per mutation operator.

    The four operators are not one difficulty. Restoring a deleted guard means
    inventing the condition that used to be there, while the other three are a
    token flipped back — so a corpus that averages well can still be two
    populations, and the average would never say so.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        parts = row["task_id"].split("-")
        groups.setdefault(parts[1] if len(parts) > 2 else "unknown", []).append(row)
    out = ["coverage by operator:"]
    for operator in sorted(groups):
        got = groups[operator]
        hits = sum(r["coverage"] for r in got)
        solved = sum(r["resolve"] for r in got)
        out.append(
            f"  {operator:<15} {hits}/{len(got)} coverage, {solved}/{len(got)} resolve"
        )
    return "\n".join(out)


def _trajectories(rows: list[dict]) -> str:
    """How long the recorded trajectories are, and how much of that was spinning.

    Reported next to the outcome rates because it gates just as much: aligning two
    six-action traces is a problem an affine-gap algorithm cannot show any
    advantage on, and a predictor cannot read the first twenty steps of a run that
    only took six.
    """
    actions = sorted(r["actions"] for r in rows)
    if not actions:
        return "no trajectories yet"
    middle = actions[len(actions) // 2]
    spins = sum(r["repeats"] for r in rows)
    turns = sum(r["turns"] for r in rows)
    stuck = sum(1 for r in rows if r["stop_reason"] == "stuck")
    buckets = Counter(min(r["actions"] // 4 * 4, 20) for r in rows)
    out = [
        f"actions per run     median {middle}, range {actions[0]}-{actions[-1]}",
        f"turns spent spinning {spins}/{turns} ({spins / turns:.0%})",
        f"ended stuck          {stuck}/{len(rows)}",
        "action-count spread:",
    ]
    for low in sorted(buckets):
        label = f"{low}-{low + 3}" if low < 20 else "20+"
        out.append(f"  {label:>6} actions  {'#' * buckets[low]:<20} {buckets[low]}")
    return "\n".join(out)


def _histogram(by_task: dict[str, list[dict]], label: str, runs: int = 5) -> str:
    """How many tasks came out all-fail, all-pass, or somewhere in between.

    The average rate cannot distinguish a corpus where every task is genuinely
    uncertain from one where half the tasks are trivial and half are impossible.
    Both average out the same, and only the second has no pairs to align.
    """
    complete = {t: rs for t, rs in by_task.items() if len(rs) >= runs}
    if not complete:
        return f"  no task has all {runs} runs in yet"
    counts = Counter(sum(r[label] for r in rs[:runs]) for t, rs in complete.items())
    out = []
    for successes in range(runs + 1):
        n = counts.get(successes, 0)
        edge = "  <- no pair" if successes in (0, runs) else ""
        out.append(f"  {successes}/{runs} passed   {'#' * n:<20} {n}{edge}")
    usable = sum(n for s, n in counts.items() if 0 < s < runs)
    out.append(f"  usable pairs: {usable} of {len(complete)} completed tasks")
    return "\n".join(out)


def store_summary(store: Path = STORE) -> str:
    db = Store(store)
    runs = db.runs()
    tasks = db.tasks()
    db.close()
    return f"{len(runs)} runs over {len(tasks)} tasks in {store}"
