from __future__ import annotations

from collections.abc import Iterator
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
    ReplayMiss,
    Session,
    StreamHandle,
    warn_if_stale,
)
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


@contextmanager
def open_session(
    run_or_name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "once",
    config: Config | None = None,
    command: list[str] | None = None,
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
    run_or_name: str, mode: RecordMode, store: str | Path, config: Config | None, overrides: Any
) -> Iterator[Session]:
    ambient = current()
    if ambient is not None:
        # Under `locus record -- ...` the wrapper owns the run, so a script that
        # opens its own session joins that one instead of starting a second.
        yield ambient
        return
    with open_session(run_or_name, store=store, mode=mode, config=config, **overrides) as s:
        yield s


@contextmanager
def record(
    name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "all",
    config: Config | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    with _entered(name, mode, store, config, overrides) as s:
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
    with _entered(run_or_name, mode, store, config, overrides) as s:
        yield s


@contextmanager
def session(
    name: str,
    *,
    store: str | Path = ".locus",
    mode: RecordMode = "once",
    config: Config | None = None,
    **overrides: Any,
) -> Iterator[Session]:
    with _entered(name, mode, store, config, overrides) as s:
        yield s
