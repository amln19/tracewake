"""Re-running a recorded attempt with one context block taken away.

The sampler is held at the seed the original run used, advanced to the turn the
fork starts generating at, so the removed block is the only thing that differs
between the two runs. Anything else — a fresh seed, a different temperature —
would leave the comparison unable to say which change moved the outcome.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import locus
from locus import Store

from . import agent, repos
from .backend import DEFAULT_MODEL, PROVIDER, LocalModel
from .repos import CORPUS_ROOT, BY_NAME
from .runner import STORE, grade, prepare
from .tasks import load

SEED_STRIDE = 1013

# Forks come from a later agent than the corpus does, and a store holding two
# agent versions cannot support a claim about why two runs diverged. They go in
# their own store so the corpus stays exactly the 192 runs it was closed at.
FORK_STORE = CORPUS_ROOT / "counterfactual"


@dataclass(frozen=True)
class Fork:
    source_run_id: str
    run_id: str
    task_id: str
    drop_tags: tuple[str, ...]
    from_turn: int
    blocks_dropped: int
    replayed_calls: int
    live_calls: int
    coverage: bool
    resolve: bool
    seconds: float
    summary: str

    def format(self) -> str:
        tags = ", ".join(self.drop_tags)
        return "\n".join(
            [
                f"task            {self.task_id}",
                f"source run      {self.source_run_id}  (in {STORE})",
                f"forked run      {self.run_id}  (in {FORK_STORE})",
                f"intervention    dropped {tags} from turn {self.from_turn}",
                f"blocks dropped  {self.blocks_dropped}",
                f"model calls     {self.replayed_calls} replayed, {self.live_calls} generated",
                f"outcome         coverage={int(self.coverage)} resolve={int(self.resolve)}",
                f"tests           {self.summary}",
                f"wall clock      {self.seconds:.0f}s",
                "",
                "compare with:   python -m bench fork-diff " + self.run_id,
            ]
        )


def fork_diff(
    run: str, store: Path = STORE, fork_store: Path = FORK_STORE, lexical: bool = False
) -> str:
    """Align a fork against the run it was forked from, across the two stores."""
    from locus.align import LexicalEmbedder, MlxEmbedder, diff_runs, format_diff
    from locus.events import InterventionEvent

    forks = Store(fork_store)
    try:
        header = forks.resolve(run)
        events = forks.events(header.run_id)
        declared = [e.event for e in events if isinstance(e.event, InterventionEvent)]
        if not declared:
            raise ValueError(
                f"run {header.run_id[:12]} in {fork_store} is not a fork — it carries no "
                f"record of what it was forked from."
            )
        source_id = declared[0].source_run_id
        db = Store(store)
        try:
            source = db.resolve(source_id)
            source_events = db.events(source.run_id)
        finally:
            db.close()
    finally:
        forks.close()

    if lexical:
        embed, model_id, revision = LexicalEmbedder(), "lexical", None
    else:
        embedder = MlxEmbedder()
        embed, model_id, revision = embedder, embedder.model_id, embedder.revision

    result = diff_runs(
        source_events,
        events,
        embed=embed,
        embedding_model=model_id,
        embedding_revision=revision,
    )
    tags = ", ".join(declared[0].drop_tags)
    return "\n".join(
        [
            f"source  {source.run_id[:12]}  {source.name}",
            f"fork    {header.run_id[:12]}  dropped {tags} from turn {declared[0].from_turn}",
            "",
            format_diff(result, good_label="SOURCE", bad_label="FORK"),
        ]
    )


def _run_index(name: str) -> int:
    _, _, tail = name.rpartition("#")
    return int(tail) if tail.isdigit() else 0


def fork(
    run: str,
    drop_tags: list[str],
    from_turn: int = 0,
    store: Path = STORE,
    fork_store: Path = FORK_STORE,
    max_steps: int = 18,
    model_id: str = DEFAULT_MODEL,
    temperature: float = 0.7,
) -> Fork:
    db = Store(store)
    source = db.resolve(run)
    db.close()
    if source.task_id is None:
        raise ValueError(
            f"run {source.run_id[:12]} carries no task id, so there is no task to rebuild a "
            f"working copy from. Fork a corpus run."
        )
    task = next((t for t in load() if t.task_id == source.task_id), None)
    if task is None:
        raise ValueError(
            f"task {source.task_id!r} is not in the current manifest, so the bug it injected "
            f"cannot be reproduced. Rebuild the manifest or pick another run."
        )

    repo = BY_NAME[task.repo]
    timeout = max(60.0, repo.baseline_seconds * 20)
    scratch = Path(tempfile.mkdtemp(prefix=f"fork-{task.task_id}-"))
    root = prepare(task, scratch / "repo")
    started = time.time()

    def run_suite() -> tuple[str, bool]:
        report = repos.run_tests(repo, root, timeout=timeout)
        return (report.output or report.summary, report.green)

    try:
        with locus.intervene(
            source.run_id,
            drop_tags=drop_tags,
            from_turn=from_turn,
            store=fork_store,
            source_store=store,
            block_network=False,
        ) as session:
            tools = agent.Tools(session, root, repo.source_dirs, run_suite)
            backend = LocalModel(
                model_id=model_id,
                temperature=temperature,
                seed=_run_index(source.name) * SEED_STRIDE,
                calls=from_turn,
            )
            handle = session.model(
                provider=PROVIDER, model_id=backend.model_id, stream_fn=backend.stream
            )
            trace = agent.run(
                session,
                handle,
                task.issue,
                tools,
                max_steps=max_steps,
                temperature=temperature,
            )
            final = repos.run_tests(repo, root, timeout=timeout)
            coverage, resolve, patch = grade(task, root, final)
            session.outcome(
                status="ok",
                usage=trace.usage,
                coverage=coverage,
                resolve=resolve,
                patch=patch or None,
                test_summary=final.summary,
            )
            result = Fork(
                source_run_id=source.run_id,
                run_id=session.run_id,
                task_id=task.task_id,
                drop_tags=tuple(sorted(drop_tags)),
                from_turn=from_turn,
                blocks_dropped=session.blocks_dropped,
                replayed_calls=session.report.matched + session.report.degraded,
                live_calls=backend.calls - from_turn,
                coverage=coverage,
                resolve=resolve,
                seconds=time.time() - started,
                summary=final.summary,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result
