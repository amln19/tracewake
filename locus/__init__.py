from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .events import (
    SCHEMA_VERSION,
    BlobRef,
    DecodeParams,
    EnvironmentEvent,
    EventMeta,
    Message,
    ModelCallEvent,
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
from .session import Completion, Player, Recorder, ReplayMiss, StreamHandle
from .store import BlobStore, Store

__all__ = [
    "SCHEMA_VERSION",
    "BlobRef",
    "BlobStore",
    "Completion",
    "DecodeParams",
    "EnvironmentEvent",
    "EventMeta",
    "Message",
    "ModelCallEvent",
    "ModelResponse",
    "OutcomeEvent",
    "Player",
    "Recorder",
    "ReplayMiss",
    "RunHeader",
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
    "hash_args",
    "hash_messages",
    "record",
    "replay",
    "run_digest",
]


@contextmanager
def record(name: str, *, store: str | Path = ".locus") -> Iterator[Recorder]:
    db = Store(store)
    run_id = uuid4().hex
    db.create_run(
        RunHeader(run_id=run_id, name=name, started_at=time.time(), status="running")
    )
    recorder = Recorder(db, run_id, name)
    try:
        yield recorder
    except BaseException:
        db.finish_run(run_id, "error", time.time())
        db.close()
        raise
    db.finish_run(run_id, "ok", time.time())
    db.close()


@contextmanager
def replay(run_id: str, *, store: str | Path = ".locus") -> Iterator[Player]:
    db = Store(store)
    db.run(run_id)
    try:
        yield Player(db, run_id)
    finally:
        db.close()
