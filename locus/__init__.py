from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cassette import export_cassette, import_cassette, read_header
from .config import (
    MATCHERS,
    RECORD_MODES,
    Config,
    RecordMode,
    configure,
    current_config,
    resolve,
)
from .events import (
    SCHEMA_VERSION,
    BlobRef,
    DecodeParams,
    EnvironmentEvent,
    EventMeta,
    FsReadEvent,
    FsWriteEvent,
    InterventionEvent,
    Message,
    ModelCallEvent,
    ModelIdentity,
    ModelResponse,
    OutcomeEvent,
    RunHeader,
    StoredEvent,
    StreamChunk,
    StreamRecord,
    ToolCallEvent,
    ToolCallRequest,
    ToolOutcome,
    Usage,
    canonical_order,
    hash_args,
    hash_messages,
    run_digest,
)
from .matching import ReplayReport
from .patches import (
    HashSeedError,
    LocusError,
    NetworkBlocked,
    block_network,
    patch_environment,
    real_time,
    real_uuid4,
    require_hash_seed,
)
from .redaction import REDACTED, Redactor
from .session import (
    CassetteStale,
    Completion,
    Intervention,
    ReplayMiss,
    Session,
    StreamHandle,
    warn_if_stale,
)
from .session import plan_intervention as _check
from .store import BlobStore, Store

__all__ = [
    "MATCHERS",
    "RECORD_MODES",
    "REDACTED",
    "SCHEMA_VERSION",
    "BlobRef",
    "BlobStore",
    "CassetteStale",
    "Completion",
    "Config",
    "DecodeParams",
    "EnvironmentEvent",
    "EventMeta",
    "FsReadEvent",
    "FsWriteEvent",
    "HashSeedError",
    "Intervention",
    "InterventionEvent",
    "LocusError",
    "Message",
    "ModelCallEvent",
    "ModelIdentity",
    "ModelResponse",
    "NetworkBlocked",
    "OutcomeEvent",
    "RecordMode",
    "Redactor",
    "ReplayMiss",
    "ReplayReport",
    "RunHeader",
    "Session",
    "Store",
    "StoredEvent",
    "StreamChunk",
    "StreamHandle",
    "StreamRecord",
    "ToolCallEvent",
    "ToolCallRequest",
    "ToolOutcome",
    "Usage",
    "canonical_order",
    "configure",
    "current",
    "current_config",
    "export_cassette",
    "hash_args",
    "hash_messages",
    "import_cassette",
    "intervene",
    "plan_intervention",
    "read_header",
    "record",
    "replay",
    "run_digest",
    "session",
]

_ambient: Session | None = None


def current() -> Session | None:
    """The session opened by the `locus` CLI for this process, if any."""
    return _ambient


def _adopt(session: Session | None) -> None:
    global _ambient
    _ambient = session


def _read_source(
    db: Store, run_id: str, source_store: str | Path | None
) -> tuple[RunHeader, list[StoredEvent]]:
    if source_store is None:
        header = db.resolve(run_id)
        return header, db.events(header.run_id)
    other = Store(source_store)
    try:
        header = other.resolve(run_id)
        return header, other.events(header.run_id)
    finally:
        other.close()


@contextmanager
def open_session(
    run_or_name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "once",
    config: Config | None = None,
    command: list[str] | None = None,
    task_id: str | None = None,
    intervention: Intervention | None = None,
    source_store: str | Path | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    if mode not in RECORD_MODES:
        raise ValueError(
            f"unknown record mode {mode!r}. Choose one of {', '.join(RECORD_MODES)}."
        )
    cfg = resolve(config, overrides)
    db = Store(store)
    stack = ExitStack()
    try:
        if intervention is not None:
            # A fork only ever reads the source's events: it serves no recorded
            # tool result and no recorded file read, so the source can live in a
            # store this run never writes to. That is what lets a closed corpus
            # be forked without being appended to.
            source, replay_events = _read_source(db, intervention.source_run_id, source_store)
            # The source governs redaction the same way replaying it would, and
            # the fork inherits its task and command so the two stay comparable.
            cfg = replace(cfg, redact=source.redacted)
            header = RunHeader(
                run_id=real_uuid4().hex,
                name=run_or_name or f"{source.name}+{intervention.label()}",
                started_at=real_time(),
                status="running",
                command=command or source.command,
                redacted=cfg.redact,
                task_id=task_id or source.task_id,
            )
            db.create_run(header)
        else:
            header = None if mode == "all" else db.find(run_or_name)
            if header is None and mode == "none":
                db.resolve(run_or_name)
            if header is None:
                header = RunHeader(
                    run_id=real_uuid4().hex,
                    name=run_or_name,
                    started_at=real_time(),
                    status="running",
                    command=command,
                    redacted=cfg.redact,
                    task_id=task_id,
                )
                db.create_run(header)
                replay_events = None
            else:
                # How the cassette was written governs how it is matched, or a
                # replay configured differently would miss every call.
                cfg = replace(cfg, redact=header.redacted)
                replay_events = db.events(header.run_id)

        active = Session(
            store=db,
            header=header,
            mode=mode,
            config=cfg,
            replay_events=replay_events,
            intervention=intervention,
        )
        if intervention is not None:
            active._append(
                InterventionEvent(
                    source_run_id=intervention.source_run_id,
                    drop_tags=sorted(intervention.drop_tags),
                    from_turn=intervention.from_turn,
                    meta=EventMeta(recorded_at=real_time()),
                )
            )
        if active.can_replay:
            require_hash_seed(cfg)
            warn_if_stale(header, cfg)
        if cfg.patch_environment:
            stack.enter_context(patch_environment(active))
        if cfg.block_network and not active.can_record:
            stack.enter_context(block_network())
    except BaseException:
        stack.close()
        db.close()
        raise

    try:
        yield active
    except BaseException:
        stack.close()
        if active.can_record:
            db.finish_run(active.run_id, "error", real_time(), active.models)
        db.close()
        raise
    stack.close()
    if active.can_record:
        db.finish_run(active.run_id, "ok", real_time(), active.models)
    db.close()


@contextmanager
def _entered(
    run_or_name: str,
    mode: RecordMode,
    store: str | Path,
    config: Config | None,
    task_id: str | None,
    overrides: Any,
) -> Iterator[Session]:
    ambient = current()
    if ambient is not None:
        # Under `locus record -- ...` the wrapper owns the run, so a script that
        # opens its own session joins that one instead of starting a second.
        yield ambient
        return
    with open_session(
        run_or_name, store=store, mode=mode, config=config, task_id=task_id, **overrides
    ) as s:
        yield s


@contextmanager
def record(
    name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "all",
    config: Config | None = None,
    task_id: str | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    with _entered(name, mode, store, config, task_id, overrides) as s:
        yield s


@contextmanager
def replay(
    run_or_name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "none",
    config: Config | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    with _entered(run_or_name, mode, store, config, None, overrides) as s:
        yield s


def plan_intervention(
    run_or_name: str,
    *,
    drop_tags: Iterable[str],
    from_turn: int = 0,
    store: str | Path = ".locus",
) -> Intervention:
    """Resolve a run and check the intervention would change something."""
    db = Store(store)
    try:
        source = db.resolve(run_or_name)
        return _check(source, db.events(source.run_id), frozenset(drop_tags), from_turn)
    finally:
        db.close()


@contextmanager
def intervene(
    run_or_name: str,
    *,
    drop_tags: Iterable[str],
    from_turn: int = 0,
    name: str = "",
    store: str | Path = ".locus",
    source_store: str | Path | None = None,
    config: Config | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    """Re-run a recorded run with context blocks removed, into a new run.

    Turns before `from_turn` replay from the source log and cost no inference,
    up to the first tool output that differs from the recorded one. From there
    the request no longer matches and the agent runs against the live model.

    The source run is never written to, and `source_store` puts the new run in a
    different store entirely so a closed corpus can be forked without growing.
    """
    intervention = plan_intervention(
        run_or_name,
        drop_tags=drop_tags,
        from_turn=from_turn,
        store=source_store if source_store is not None else store,
    )
    with open_session(
        name,
        store=store,
        mode="new_episodes",
        config=config,
        intervention=intervention,
        source_store=source_store,
        **overrides,
    ) as s:
        yield s


@contextmanager
def session(
    name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "once",
    config: Config | None = None,
    task_id: str | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    with _entered(name, mode, store, config, task_id, overrides) as s:
        yield s
