from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer

from .cassette import export_cassette, import_cassette, read_header
from .config import RECORD_MODES, RecordMode
from .patches import LocusError
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

    stack = ExitStack()
    session = stack.enter_context(
        locus.open_session(
            os.environ["LOCUS_TARGET"],
            store=os.environ["LOCUS_STORE"],
            mode=os.environ["LOCUS_MODE"],  # type: ignore[arg-type]
            command=json.loads(os.environ["LOCUS_COMMAND"]),
            redact=os.environ["LOCUS_REDACT"] == "1",
        )
    )
    locus._adopt(session)
    Path(os.environ["LOCUS_RUN_ID_FILE"]).write_text(session.run_id, encoding="utf-8")
    atexit.register(stack.close)


def _run_child(
    target: str, command: list[str], store: Path, mode: RecordMode, redact: bool = True
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="locus-bootstrap-") as tmp:
        Path(tmp, "sitecustomize.py").write_text(BOOTSTRAP, encoding="utf-8")
        id_file = Path(tmp, "run-id")
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
            LOCUS_REDACT="1" if redact else "0",
        )
        completed = subprocess.run(command, env=env, check=False)
        run_id = id_file.read_text(encoding="utf-8").strip() if id_file.exists() else ""
    if not run_id:
        raise typer.BadParameter(
            f"{command[0]!r} exited without opening a locus session. The wrapper injects "
            f"itself through sitecustomize, which only applies to Python programs."
        )
    return completed.returncode, run_id


def _report(store: Store, run_id: str, code: int) -> None:
    header = store.run(run_id)
    models = ", ".join(f"{m.provider}/{m.model_id}" for m in header.models) or "no model calls"
    typer.echo(f"run {run_id}  cassette {header.name!r}  {models}")
    if code != 0:
        typer.echo(f"the recorded program exited {code}", err=True)


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
    code, run_id = _run_child(target, command, store, mode, redact=not no_redact)  # type: ignore[arg-type]
    db = Store(store)
    db.finish_run(run_id, "ok" if code == 0 else "error", time.time())
    _report(db, run_id, code)
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
    code, _ = _run_child(header.run_id, target, store, "none")
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


@app.command("diff")
def diff_(
    good: Annotated[str, typer.Argument(help="Passing run id or cassette name.")],
    bad: Annotated[str, typer.Argument(help="Failing run id or cassette name.")],
    store: StoreOption = Path(".locus"),
    lexical: Annotated[
        bool,
        typer.Option(
            "--lexical",
            help="Skip the embedding model and score reasoning text lexically.",
        ),
    ] = False,
) -> None:
    """Align two runs and print the step where they stopped agreeing."""
    from .align import (
        EMBEDDING_MODEL,
        EMBEDDING_REVISION,
        LexicalEmbedder,
        MlxEmbedder,
        diff_runs,
        format_diff,
    )

    db = Store(store)
    good_header = db.resolve(good)
    bad_header = db.resolve(bad)
    good_events = db.events(good_header.run_id)
    bad_events = db.events(bad_header.run_id)
    db.close()

    if lexical:
        embed = LexicalEmbedder()
        model_id = revision = None
    else:
        embedder = MlxEmbedder()
        embed = embedder
        model_id, revision = embedder.model_id, embedder.revision

    result = diff_runs(
        good_events,
        bad_events,
        embed=embed,
        embedding_model=model_id or ("lexical" if lexical else EMBEDDING_MODEL),
        embedding_revision=revision if not lexical else None,
    )
    typer.echo(
        format_diff(
            result,
            good_label=f"GOOD {good_header.run_id[:8]}",
            bad_label=f"BAD {bad_header.run_id[:8]}",
        )
    )
    if result.excluded_by_length:
        raise typer.Exit(2)


def main() -> None:
    # A traceback is not a user interface. Locus raises these to say what failed
    # and what to do about it, so the CLI prints the message and nothing else.
    try:
        app()
    except (KeyError, ValueError, LocusError) as exc:
        message = exc.args[0] if exc.args else str(exc)
        typer.echo(f"locus: {message}", err=True)
        sys.exit(1)
