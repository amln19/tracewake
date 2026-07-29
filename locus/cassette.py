from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel

from .events import (
    EVENT_ADAPTER,
    SCHEMA_VERSION,
    AnyEvent,
    FsReadEvent,
    FsWriteEvent,
    ModelIdentity,
    RunHeader,
    StoredEvent,
    ToolCallEvent,
    run_digest,
)
from .store import Store

CASSETTE_FORMAT = 1
CASSETTE_FILE = "cassette.jsonl"
BLOB_DIR = "blobs"


class CassetteHeader(BaseModel):
    """Line one of a cassette.

    Carries what a reader needs to judge whether replaying it still means
    anything: which model produced it, through which provider, and when.
    """

    format_version: int = CASSETTE_FORMAT
    locus_version: str
    schema_version: int
    run_id: str
    name: str
    recorded_at: float
    finished_at: float | None = None
    status: str
    models: list[ModelIdentity]
    command: list[str] | None = None
    redacted: bool = True
    task_id: str | None = None
    event_count: int
    digest: str


def _blob_digests(events: list[StoredEvent]) -> set[str]:
    found: set[str] = set()
    for stored in events:
        match stored.event:
            case ToolCallEvent(result=ref):
                found.add(ref.digest)
            case FsReadEvent(content=ref) | FsWriteEvent(content=ref) if ref is not None:
                found.add(ref.digest)
    return found


def _line(stored: StoredEvent) -> str:
    data = json.loads(stored.event.canonical_bytes())
    data["meta"] = stored.event.meta.model_dump(mode="json")
    data["seq"] = stored.seq
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def export_cassette(store: Store, run_or_name: str, dest: str | Path) -> Path:
    header = store.resolve(run_or_name)
    events = store.events(header.run_id)
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)

    cassette = CassetteHeader(
        locus_version=version("locus"),
        schema_version=SCHEMA_VERSION,
        run_id=header.run_id,
        name=header.name,
        recorded_at=header.started_at,
        finished_at=header.finished_at,
        status=header.status,
        models=header.models,
        command=header.command,
        redacted=header.redacted,
        task_id=header.task_id,
        event_count=len(events),
        digest=run_digest(events),
    )
    lines = [cassette.model_dump_json()] + [_line(e) for e in events]
    (out / CASSETTE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    blobs = out / BLOB_DIR
    for digest in sorted(_blob_digests(events)):
        target = blobs / digest[:2] / digest[2:4] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(store.blobs.get(digest))
    return out


def read_header(source: str | Path) -> CassetteHeader:
    path = Path(source)
    if path.is_dir():
        path = path / CASSETTE_FILE
    with path.open(encoding="utf-8") as fh:
        first = fh.readline()
    if not first.strip():
        raise ValueError(f"{path} is empty; a cassette starts with a header line.")
    return CassetteHeader.model_validate_json(first)


def _events(path: Path) -> list[tuple[int, AnyEvent]]:
    out: list[tuple[int, AnyEvent]] = []
    with path.open(encoding="utf-8") as fh:
        fh.readline()
        for number, line in enumerate(fh, start=2):
            if not line.strip():
                continue
            data = json.loads(line)
            seq = data.pop("seq")
            try:
                out.append((seq, EVENT_ADAPTER.validate_python(data)))
            except ValueError as exc:
                raise ValueError(
                    f"{path} line {number} is not a valid locus event: {exc}"
                ) from exc
    return out


def import_cassette(source: str | Path, store: Store) -> RunHeader:
    path = Path(source)
    root = path if path.is_dir() else path.parent
    cassette = read_header(path)
    if cassette.format_version != CASSETTE_FORMAT:
        raise ValueError(
            f"{path} is cassette format {cassette.format_version}, but this locus reads "
            f"format {CASSETTE_FORMAT}."
        )
    if cassette.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"{path} holds schema version {cassette.schema_version}, but this locus reads "
            f"version {SCHEMA_VERSION}. Re-record the run with a matching locus."
        )
    if store.find(cassette.run_id) is not None:
        raise ValueError(
            f"run {cassette.run_id} is already in {store.root}. Importing would record it "
            f"twice; delete it first or import into a different --store."
        )

    blobs = root / BLOB_DIR
    for blob in sorted(blobs.rglob("*")) if blobs.is_dir() else []:
        if blob.is_file():
            store.blobs.put(blob.read_bytes())

    header = RunHeader(
        run_id=cassette.run_id,
        name=cassette.name,
        started_at=cassette.recorded_at,
        finished_at=cassette.finished_at,
        status=cassette.status,
        models=cassette.models,
        command=cassette.command,
        redacted=cassette.redacted,
        task_id=cassette.task_id,
    )
    store.create_run(header)
    events = _events(root / CASSETTE_FILE if path.is_dir() else path)
    for _, event in sorted(events, key=lambda pair: pair[0]):
        store.append(header.run_id, event)

    imported = store.events(header.run_id)
    digest = run_digest(imported)
    if digest != cassette.digest:
        raise ValueError(
            f"{path} did not survive the round trip: the imported run hashes to "
            f"{digest[:12]} but the cassette header says {cassette.digest[:12]}. The file "
            f"has been edited or truncated; re-export it."
        )
    return header
