from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from .align import DiffResult, LexicalEmbedder, MlxEmbedder, diff_runs, format_diff
from .cassette import export_cassette, import_cassette, read_header
from .config import RECORD_MODES, RecordMode
from .events import RunHeader, StoredEvent
from .matching import ReplayReport
from .patches import LocusError
from .report import PAYLOAD_BUDGET, write_report
from .session import Intervention
from .store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Record an agent run once, replay it offline for free.",
)

BOOTSTRAP = """\
import os

if os.environ.get("LOCUS_BOOTSTRAP") == "1":
    from locus.cli import bootstrap_from_env

    bootstrap_from_env()
"""

StoreOption = Annotated[Path, typer.Option("--store", help="Store directory.")]


def bootstrap_from_env() -> None:
    """Open the wrapper's session inside the child process.

    Injected through `sitecustomize`, so the patches are live before the target
    program imports anything. A script that opens its own session joins this one
    rather than starting a second.
    """
    import locus

    spec = os.environ.get("LOCUS_INTERVENTION")
    intervention = None
    if spec:
        fields = json.loads(spec)
        intervention = locus.Intervention(
            source_run_id=fields["source_run_id"],
            drop_tags=frozenset(fields["drop_tags"]),
            from_turn=fields["from_turn"],
        )
    stack = ExitStack()
    session = stack.enter_context(
        locus.open_session(
            os.environ["LOCUS_TARGET"],
            store=os.environ["LOCUS_STORE"],
            mode=os.environ["LOCUS_MODE"],  # type: ignore[arg-type]
            command=json.loads(os.environ["LOCUS_COMMAND"]),
            redact=os.environ["LOCUS_REDACT"] == "1",
            intervention=intervention,
            source_store=os.environ.get("LOCUS_SOURCE_STORE"),
        )
    )
    locus._adopt(session)
    Path(os.environ["LOCUS_RUN_ID_FILE"]).write_text(session.run_id, encoding="utf-8")
    # Read now, not at exit: the recorded program owns os.environ in between and
    # is free to clear it.
    report_path = Path(os.environ["LOCUS_REPORT_FILE"])

    def finish() -> None:
        # The counts only settle once the session closes, and the parent cannot
        # see them at all — the run happens in this process. Hand them back over
        # the same file channel that carries the run id.
        stack.close()
        report_path.write_text(json.dumps(asdict(session.report)), encoding="utf-8")

    atexit.register(finish)


def _run_child(
    target: str,
    command: list[str],
    store: Path,
    mode: RecordMode,
    redact: bool = True,
    intervention: Intervention | None = None,
    source_store: Path | None = None,
) -> tuple[int, str, ReplayReport | None]:
    with tempfile.TemporaryDirectory(prefix="locus-bootstrap-") as tmp:
        Path(tmp, "sitecustomize.py").write_text(BOOTSTRAP, encoding="utf-8")
        id_file = Path(tmp, "run-id")
        report_file = Path(tmp, "replay-report")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([tmp, *filter(None, [env.get("PYTHONPATH")])])
        # Set here rather than checked, because the interpreter fixes hash
        # randomization at startup and the child has not started yet.
        env["PYTHONHASHSEED"] = "0"
        env.update(
            LOCUS_BOOTSTRAP="1",
            LOCUS_TARGET=target,
            LOCUS_STORE=str(store),
            LOCUS_MODE=mode,
            LOCUS_COMMAND=json.dumps(command),
            LOCUS_RUN_ID_FILE=str(id_file),
            LOCUS_REPORT_FILE=str(report_file),
            LOCUS_REDACT="1" if redact else "0",
        )
        if intervention is not None:
            env["LOCUS_INTERVENTION"] = json.dumps(
                {
                    "source_run_id": intervention.source_run_id,
                    "drop_tags": sorted(intervention.drop_tags),
                    "from_turn": intervention.from_turn,
                }
            )
            if source_store is not None:
                env["LOCUS_SOURCE_STORE"] = str(source_store)
        completed = subprocess.run(command, env=env, check=False)
        run_id = id_file.read_text(encoding="utf-8").strip() if id_file.exists() else ""
        # Absent when the child died before its atexit hooks ran. The counts are
        # then unknown, which is not the same as zero, so say nothing about them.
        raw = report_file.read_text(encoding="utf-8") if report_file.exists() else ""
    if not run_id:
        raise typer.BadParameter(
            f"{command[0]!r} exited without opening a locus session. The wrapper injects "
            f"itself through sitecustomize, which only applies to Python programs."
        )
    report = ReplayReport(**json.loads(raw)) if raw else None
    return completed.returncode, run_id, report


def _report(store: Store, run_id: str, code: int, replay: ReplayReport | None = None) -> None:
    header = store.run(run_id)
    models = ", ".join(f"{m.provider}/{m.model_id}" for m in header.models) or "no model calls"
    typer.echo(f"run {run_id}  cassette {header.name!r}  {models}")
    _echo_replay(replay)
    if code != 0:
        typer.echo(f"the recorded program exited {code}", err=True)


def _echo_replay(replay: ReplayReport | None) -> None:
    """Say how much of the log the run answered from, and how surely.

    `degraded` is the one to read: those calls matched without `messages_hash`,
    so the request was accepted without proving it is the one that was recorded.
    The individual misses are left out — in `none` mode ReplayMiss has already
    described the one that stopped the run, and in `new_episodes` a miss is the
    branch the caller asked for, not a fault.
    """
    if replay is not None and replay.recorded_calls:
        typer.echo(replay.summary())


@app.command()
def record(
    command: Annotated[list[str], typer.Argument(help="Program to run, after `--`.")],
    name: Annotated[str, typer.Option("--name", help="Cassette name.")] = "",
    store: StoreOption = Path(".locus"),
    mode: Annotated[str, typer.Option("--mode", help=f"One of {', '.join(RECORD_MODES)}.")] = "all",
    no_redact: Annotated[
        bool,
        typer.Option(
            "--no-redact",
            help="Write credentials and absolute home paths to the log unscrubbed.",
        ),
    ] = False,
) -> None:
    """Run a program and record everything it consumes."""
    if mode not in RECORD_MODES:
        raise typer.BadParameter(f"unknown mode {mode!r}; choose from {', '.join(RECORD_MODES)}.")
    target = name or Path(command[0]).name
    code, run_id, replay = _run_child(
        target, command, store, mode, redact=not no_redact  # type: ignore[arg-type]
    )
    db = Store(store)
    db.finish_run(run_id, "ok" if code == 0 else "error", time.time())
    _report(db, run_id, code, replay)
    db.close()
    raise typer.Exit(code)


@app.command()
def replay(
    run: Annotated[str, typer.Argument(help="Run id or cassette name.")],
    store: StoreOption = Path(".locus"),
    command: Annotated[list[str] | None, typer.Argument(help="Override the command.")] = None,
) -> None:
    """Re-run a recorded program against its log, with the network disabled."""
    db = Store(store)
    header = db.resolve(run)
    db.close()
    target = command or header.command
    if not target:
        raise typer.BadParameter(
            f"run {header.run_id} was not recorded through `locus record`, so it has no "
            f"command to re-run. Pass the program after the run id."
        )
    code, _, replay = _run_child(header.run_id, target, store, "none")
    _echo_replay(replay)
    raise typer.Exit(code)


@app.command("intervene")
def intervene_(
    run: Annotated[str, typer.Argument(help="Run id or cassette name to fork.")],
    command: Annotated[list[str] | None, typer.Argument(help="Program to re-run, after `--`.")] = None,
    drop_tag: Annotated[
        list[str] | None,
        typer.Option("--drop-tag", help="Provenance tag to remove from the context."),
    ] = None,
    from_step: Annotated[
        int, typer.Option("--from-step", help="First model call to change, numbered from 0.")
    ] = 0,
    name: Annotated[str, typer.Option("--name", help="Cassette name for the forked run.")] = "",
    store: StoreOption = Path(".locus"),
    source_store: Annotated[
        Path | None,
        typer.Option("--source-store", help="Read the run from here and write the fork to --store."),
    ] = None,
) -> None:
    """Re-run a recorded run with context blocks removed, into a new run.

    Turns before --from-step replay from the recorded log and cost no inference,
    up to the first tool output that differs from the recorded one. From there
    the request no longer matches, so the run continues against the live model.
    The original run is never written to.
    """
    import locus

    if not drop_tag:
        raise typer.BadParameter("pass --drop-tag TAG to say what to neutralize")

    origin = source_store or store
    db = Store(origin)
    header = db.resolve(run)
    db.close()
    # Fails here rather than after the inference it would have taken to find out.
    plan = locus.plan_intervention(run, drop_tags=drop_tag, from_turn=from_step, store=origin)

    target = command or header.command
    if not target:
        raise typer.BadParameter(
            f"run {header.run_id[:12]} was not recorded through `locus record`, so it has no "
            f"command to re-run. Pass the program after the run id."
        )
    typer.echo(f"forking {header.run_id[:12]} — {plan.describe()}")
    code, forked, replay = _run_child(
        name or f"{header.name}+{plan.label()}",
        target,
        store,
        "new_episodes",
        intervention=plan,
        source_store=source_store,
    )
    db = Store(store)
    db.finish_run(forked, "ok" if code == 0 else "error", time.time())
    _echo_replay(replay)
    typer.echo(f"recorded {forked}  —  compare with: locus diff {run} {forked}")
    db.close()
    raise typer.Exit(code)


@app.command("export")
def export_(
    run: Annotated[str, typer.Argument(help="Run id or cassette name.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Destination directory.")],
    store: StoreOption = Path(".locus"),
) -> None:
    """Write a run to a git-committable JSONL cassette."""
    db = Store(store)
    destination = export_cassette(db, run, out)
    header = read_header(destination)
    db.close()
    typer.echo(f"exported {header.event_count} events to {destination} ({header.digest[:12]})")


@app.command("import")
def import_(
    source: Annotated[Path, typer.Argument(help="Cassette directory.")],
    store: StoreOption = Path(".locus"),
) -> None:
    """Read a JSONL cassette into the working store."""
    db = Store(store)
    header = import_cassette(source, db)
    db.close()
    typer.echo(f"imported run {header.run_id} as cassette {header.name!r}")


@app.command("ls")
def ls(store: StoreOption = Path(".locus")) -> None:
    """List recorded runs, newest first."""
    db = Store(store)
    runs = db.runs()
    if not runs:
        typer.echo(f"no runs in {store}")
    for header in runs:
        models = ",".join(m.model_id for m in header.models) or "-"
        typer.echo(f"{header.run_id[:12]}  {header.status:<7}  {models:<24}  {header.name}")
    db.close()


LexicalOption = Annotated[
    bool,
    typer.Option("--lexical", help="Skip the embedding model and score reasoning text lexically."),
]


StoreBOption = Annotated[
    Path | None,
    typer.Option("--store-b", help="Store holding the second run, if it is not in --store."),
]


def _align_pair(
    db: Store, db_b: Store, good: str, bad: str, lexical: bool
) -> tuple[RunHeader, list[StoredEvent], RunHeader, list[StoredEvent], DiffResult]:
    good_header = db.resolve(good)
    bad_header = db_b.resolve(bad)
    good_events = db.events(good_header.run_id)
    bad_events = db_b.events(bad_header.run_id)

    if lexical:
        embed = LexicalEmbedder()
        model_id = "lexical"
        revision = None
    else:
        embedder = MlxEmbedder()
        embed = embedder
        model_id, revision = embedder.model_id, embedder.revision

    result = diff_runs(
        good_events,
        bad_events,
        embed=embed,
        embedding_model=model_id,
        embedding_revision=revision,
    )
    return good_header, good_events, bad_header, bad_events, result


@app.command("diff")
def diff_(
    good: Annotated[str, typer.Argument(help="Passing run id or cassette name.")],
    bad: Annotated[str, typer.Argument(help="Failing run id or cassette name.")],
    store: StoreOption = Path(".locus"),
    store_b: StoreBOption = None,
    lexical: LexicalOption = False,
) -> None:
    """Align two runs and print the step where they stopped agreeing."""
    db = Store(store)
    db_b = Store(store_b) if store_b else db
    good_header, _, bad_header, _, result = _align_pair(db, db_b, good, bad, lexical)
    if db_b is not db:
        db_b.close()
    db.close()

    typer.echo(
        format_diff(
            result,
            good_label=f"GOOD {good_header.run_id[:8]}",
            bad_label=f"BAD {bad_header.run_id[:8]}",
        )
    )
    if result.excluded_by_length:
        raise typer.Exit(2)


@app.command("view")
def view(
    good: Annotated[str, typer.Argument(help="Passing run id or cassette name.")],
    bad: Annotated[str, typer.Argument(help="Failing run id or cassette name.")],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Destination HTML file.")
    ] = Path("locus-report.html"),
    store: StoreOption = Path(".locus"),
    store_b: StoreBOption = None,
    lexical: LexicalOption = False,
    max_bytes: Annotated[
        int,
        typer.Option("--max-bytes", help="Cap on the embedded comparison data."),
    ] = PAYLOAD_BUDGET,
) -> None:
    """Write a self-contained HTML report comparing two runs."""
    db = Store(store)
    db_b = Store(store_b) if store_b else db
    good_header, good_events, bad_header, bad_events, result = _align_pair(
        db, db_b, good, bad, lexical
    )
    payload = write_report(
        out,
        good_header,
        good_events,
        bad_header,
        bad_events,
        result,
        blobs=db.blobs,
        blobs_b=db_b.blobs,
        store_path=str(store),
        store_path_b=str(store_b) if store_b else None,
        budget=max_bytes,
    )
    if db_b is not db:
        db_b.close()
    db.close()

    size = out.stat().st_size
    where = (
        "no standing divergence"
        if result.divergence is None
        else f"divergence at failing step {result.divergence}"
    )
    typer.echo(f"wrote {out} ({size / 1e6:.2f} MB) — {where}")
    clipped = payload["truncation"]["blocks"]
    if clipped:
        typer.echo(
            f"clipped {clipped} context blocks to stay under the "
            f"{max_bytes / 1e6:.2f} MB embedded payload cap; full text is in {store}"
        )


@app.command("pprof")
def pprof_(
    run: Annotated[str, typer.Argument(help="Run id or cassette name.")],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Gzipped pprof profile (.pb.gz)."),
    ] = None,
    view: Annotated[
        str,
        typer.Option("--view", help="Profile kind. Only 'tokens' is supported."),
    ] = "tokens",
    top: Annotated[
        int | None,
        typer.Option("--top", help="Print the N heaviest leaves instead of writing a file."),
    ] = None,
    store: StoreOption = Path(".locus"),
) -> None:
    """Export a standard pprof profile of token spend for a run."""
    from .pprof import format_top, write_token_profile

    if view != "tokens":
        raise typer.BadParameter(
            f"unknown view {view!r}; only 'tokens' is supported. "
            f"Example: locus pprof <run> --view tokens -o run.pb.gz"
        )
    if out is None and top is None:
        raise typer.BadParameter("pass -o path.pb.gz to write a profile, or --top N for a summary")

    db = Store(store)
    header = db.resolve(run)
    events = db.events(header.run_id)
    db.close()

    if top is not None:
        typer.echo(format_top(header, events, n=top))
        return

    assert out is not None
    input_tokens, output_tokens = write_token_profile(out, header, events)
    typer.echo(
        f"wrote {out} ({out.stat().st_size} bytes) — "
        f"{input_tokens} input + {output_tokens} output tokens"
    )


@app.command("otel")
def otel_(
    run: Annotated[str, typer.Argument(help="Run id or cassette name.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="OTLP/JSON trace file.")],
    store: StoreOption = Path(".locus"),
) -> None:
    """Export a run as OTLP/JSON spans under the GenAI semantic conventions."""
    from .otel import write_spans

    db = Store(store)
    header = db.resolve(run)
    events = db.events(header.run_id)
    db.close()

    spans = write_spans(out, header, events)
    typer.echo(f"wrote {out} ({spans} spans, one trace for run {header.run_id[:12]})")


def main() -> None:
    # A traceback is not a user interface. Locus raises these to say what failed
    # and what to do about it, so the CLI prints the message and nothing else.
    try:
        app()
    except (KeyError, ValueError, LocusError) as exc:
        message = exc.args[0] if exc.args else str(exc)
        typer.echo(f"locus: {message}", err=True)
        sys.exit(1)
