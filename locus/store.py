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
    ModelIdentity,
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
    schema_version INTEGER NOT NULL,
    models_json    TEXT NOT NULL DEFAULT '[]',
    command_json   TEXT,
    redacted       INTEGER NOT NULL DEFAULT 1,
    task_id        TEXT
);

CREATE INDEX IF NOT EXISTS runs_by_name ON runs(name, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id, started_at);

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

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

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
        self._open_schema()
        self._lock = threading.Lock()

    def _open_schema(self) -> None:
        existing = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if existing is None:
            self._db.executescript(SCHEMA_SQL)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"the store at {self.root} was written in format {version or 1}, but this "
                f"locus reads format {SCHEMA_VERSION}. Export anything you need from it "
                f"with the older version, or point --store at a new directory."
            )
        self._db.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._db.close()

    def create_run(self, header: RunHeader) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, name, started_at, finished_at, status, "
                "schema_version, models_json, command_json, redacted, task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    header.run_id,
                    header.name,
                    header.started_at,
                    header.finished_at,
                    header.status,
                    header.schema_version,
                    json.dumps([m.model_dump(mode="json") for m in header.models]),
                    json.dumps(header.command) if header.command is not None else None,
                    int(header.redacted),
                    header.task_id,
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        finished_at: float,
        models: list[ModelIdentity] | None = None,
    ) -> None:
        with self._lock, self._db:
            if models is not None:
                self._db.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, models_json = ? "
                    "WHERE run_id = ?",
                    (
                        status,
                        finished_at,
                        json.dumps([m.model_dump(mode="json") for m in models]),
                        run_id,
                    ),
                )
            else:
                self._db.execute(
                    "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                    (status, finished_at, run_id),
                )

    def _header(self, row: sqlite3.Row) -> RunHeader:
        data = dict(row)
        data["models"] = json.loads(data.pop("models_json"))
        command = data.pop("command_json")
        data["command"] = json.loads(command) if command is not None else None
        data["redacted"] = bool(data["redacted"])
        header = RunHeader(**data)
        if header.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"run {header.run_id} was written with schema version "
                f"{header.schema_version}, but this locus reads version {SCHEMA_VERSION}. "
                f"Re-record the run."
            )
        return header

    def run(self, run_id: str) -> RunHeader:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            known = [r[0] for r in self._db.execute("SELECT run_id FROM runs").fetchall()]
            raise KeyError(
                f"no run {run_id!r} in {self.root}. Known runs: {known or 'none'}."
            )
        return self._header(row)

    def latest_named(self, name: str) -> RunHeader | None:
        """The cassette a name refers to.

        A name identifies a cassette; re-recording under the same name adds a run
        rather than replacing one, so the newest wins and the superseded
        recordings stay addressable by id for comparison later.
        """
        row = self._db.execute(
            "SELECT * FROM runs WHERE name = ? ORDER BY started_at DESC, run_id DESC LIMIT 1",
            (name,),
        ).fetchone()
        return self._header(row) if row is not None else None

    def find(self, run_or_name: str) -> RunHeader | None:
        row = self._db.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_or_name,)
        ).fetchone()
        if row is not None:
            return self._header(row)
        named = self.latest_named(run_or_name)
        if named is not None:
            return named
        # Whatever `locus ls` prints has to be valid input to `locus replay`,
        # and it prints an abbreviated id.
        rows = self._db.execute(
            "SELECT * FROM runs WHERE run_id LIKE ? || '%'", (run_or_name,)
        ).fetchall()
        if len(rows) > 1:
            ids = ", ".join(r["run_id"][:12] for r in rows)
            raise KeyError(
                f"{run_or_name!r} matches more than one run in {self.root}: {ids}. "
                f"Use more characters of the run id."
            )
        return self._header(rows[0]) if rows else None

    def resolve(self, run_or_name: str) -> RunHeader:
        header = self.find(run_or_name)
        if header is not None:
            return header
        known = [
            f"{r['run_id'][:8]} {r['name']}"
            for r in self._db.execute(
                "SELECT run_id, name FROM runs ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
        ]
        raise KeyError(
            f"no run or cassette named {run_or_name!r} in {self.root}. "
            f"Known runs: {'; '.join(known) if known else 'none'}."
        )

    def runs(self, task_id: str | None = None) -> list[RunHeader]:
        if task_id is None:
            rows = self._db.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
            return [self._header(r) for r in rows]
        rows = self._db.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,)
        ).fetchall()
        return [self._header(r) for r in rows]

    def tasks(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT task_id FROM runs WHERE task_id IS NOT NULL ORDER BY task_id"
        ).fetchall()
        return [r[0] for r in rows]

    def append(self, run_id: str, event: AnyEvent) -> int:
        canonical = event.canonical_bytes().decode("utf-8")
        meta = event.meta.model_dump_json()
        call_id = event.call_id if isinstance(event, ModelCallEvent) else None
        model_id = event.model_id if isinstance(event, ModelCallEvent) else None
        messages_hash = event.messages_hash if isinstance(event, ModelCallEvent) else None
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
                    event.tool_call_id,
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
