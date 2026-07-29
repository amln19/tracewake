from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path

from .events import (
    EVENT_ADAPTER,
    SCHEMA_VERSION,
    AnyEvent,
    BlobRef,
    ModelCallEvent,
    RunHeader,
    StoredEvent,
    ToolCallEvent,
    sha256_hex,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    status         TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    seq            INTEGER NOT NULL,
    type           TEXT NOT NULL,
    call_id        TEXT,
    parent_call_id TEXT,
    tool_call_id   TEXT,
    batch_index    INTEGER,
    model_id       TEXT,
    messages_hash  TEXT,
    canonical_json TEXT NOT NULL,
    meta_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> BlobRef:
        digest = sha256_hex(data)
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with open(fd, "wb") as fh:
                fh.write(data)
            Path(tmp).replace(path)
        return BlobRef(digest=digest, size=len(data))

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        if not path.exists():
            raise KeyError(
                f"blob {digest} is missing from {self.root}. The database and the blob "
                f"directory must be copied together; a run cannot be replayed from the "
                f"database alone."
            )
        return path.read_bytes()

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / digest


class Store:
    def __init__(self, root: str | Path = ".locus") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.root / "blobs")
        # Parallel tool batches append from worker threads, so the connection is
        # shared across threads and every write goes through _lock.
        self._db = sqlite3.connect(self.root / "locus.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA_SQL)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def create_run(self, header: RunHeader) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, name, started_at, finished_at, status, "
                "schema_version) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    header.run_id,
                    header.name,
                    header.started_at,
                    header.finished_at,
                    header.status,
                    header.schema_version,
                ),
            )

    def finish_run(self, run_id: str, status: str, finished_at: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, finished_at, run_id),
            )

    def run(self, run_id: str) -> RunHeader:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            known = [r[0] for r in self._db.execute("SELECT run_id FROM runs").fetchall()]
            raise KeyError(
                f"no run {run_id!r} in {self.root}. Known runs: {known or 'none'}."
            )
        header = RunHeader(**dict(row))
        if header.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"run {run_id} was written with schema version {header.schema_version}, "
                f"but this locus reads version {SCHEMA_VERSION}. Re-record the run."
            )
        return header

    def append(self, run_id: str, event: AnyEvent) -> int:
        canonical = event.canonical_bytes().decode("utf-8")
        meta = event.meta.model_dump_json()
        call_id = event.call_id if isinstance(event, ModelCallEvent) else None
        model_id = event.model_id if isinstance(event, ModelCallEvent) else None
        messages_hash = event.messages_hash if isinstance(event, ModelCallEvent) else None
        tool_call_id = event.tool_call_id if isinstance(event, ToolCallEvent) else None
        batch_index = event.batch_index if isinstance(event, ToolCallEvent) else None
        with self._lock, self._db:
            (seq,) = self._db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
            self._db.execute(
                "INSERT INTO events (run_id, seq, type, call_id, parent_call_id, "
                "tool_call_id, batch_index, model_id, messages_hash, canonical_json, "
                "meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    seq,
                    event.type,
                    call_id,
                    event.parent_call_id,
                    tool_call_id,
                    batch_index,
                    model_id,
                    messages_hash,
                    canonical,
                    meta,
                ),
            )
        return seq

    def events(self, run_id: str) -> list[StoredEvent]:
        rows = self._db.execute(
            "SELECT seq, canonical_json, meta_json FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        out: list[StoredEvent] = []
        for row in rows:
            data = json.loads(row["canonical_json"])
            data["meta"] = json.loads(row["meta_json"])
            out.append(
                StoredEvent(
                    run_id=run_id, seq=row["seq"], event=EVENT_ADAPTER.validate_python(data)
                )
            )
        return out
