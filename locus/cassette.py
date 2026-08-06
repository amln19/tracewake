from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .events import (
    EVENT_ADAPTER,
    SCHEMA_VERSION,
    BlobRef,
    ModelIdentity,
    RunHeader,
    StoredEvent,
    run_digest,
    sha256_hex,
)
from .store import Store

CASSETTE_FORMAT = 1
CASSETTE_FILE = "cassette.jsonl"
BLOB_DIR = "blobs"

_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class CassetteHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: int = CASSETTE_FORMAT
    locus_version: str
    schema_version: int
    run_id: str
    name: str
    recorded_at: float
    finished_at: float | None = None
    status: Literal["running", "ok", "error"]
    models: list[ModelIdentity]
    command: list[str] | None = None
    redacted: bool = True
    task_id: str | None = None
    event_count: int = Field(ge=0)
    digest: _Digest


@dataclass(frozen=True)
class _ValidatedCassette:
    source: Path
    header: CassetteHeader
    events: tuple[StoredEvent, ...]
    blobs: dict[str, bytes]


def _blob_refs(events: list[StoredEvent] | tuple[StoredEvent, ...]) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}

    def walk(value: object) -> None:
        match value:
            case BlobRef(digest=digest, size=size):
                found.setdefault(digest, set()).add(size)
            case BaseModel():
                for child in value.__dict__.values():
                    walk(child)
            case list() | tuple():
                for child in value:
                    walk(child)

    for stored in events:
        walk(stored.event)
    return found


def _blob_digests(events: list[StoredEvent] | tuple[StoredEvent, ...]) -> set[str]:
    return set(_blob_refs(events))


def _line(stored: StoredEvent) -> str:
    data = json.loads(stored.event.canonical_bytes())
    data["meta"] = stored.event.meta.model_dump(mode="json")
    data["seq"] = stored.seq
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _header_path(source: str | Path) -> Path:
    path = Path(source)
    return path / CASSETTE_FILE if path.is_dir() else path


def read_header(source: str | Path) -> CassetteHeader:
    path = _header_path(source)
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"cassette {source} expected entry {path} to be a regular file; "
            f"actual entry is {'a symlink' if path.is_symlink() else 'missing or not a file'}."
        )
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"cassette {source} entry {path} expected UTF-8; actual decode failed: {exc}."
        ) from exc
    if not first.strip():
        raise ValueError(
            f"cassette {source} entry {path} expected a header on line 1; actual line is empty."
        )
    try:
        return CassetteHeader.model_validate_json(first)
    except (ValidationError, ValueError) as exc:
        raise ValueError(
            f"cassette {source} entry {path} expected a valid header on line 1; actual value "
            f"failed validation: {exc}."
        ) from exc


def _read_events(path: Path, header: CassetteHeader, source: Path) -> list[StoredEvent]:
    parsed: list[tuple[int, Any, int]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            fh.readline()
            for number, line in enumerate(fh, start=2):
                if not line.strip():
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected an event; "
                        "actual line is blank."
                    )
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected JSON; "
                        f"actual value failed to parse: {exc}."
                    ) from exc
                if not isinstance(data, dict):
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected an event object; "
                        f"actual value is {type(data).__name__}."
                    )
                if "seq" not in data:
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected sequence "
                        "number; actual value is missing."
                    )
                seq = data.pop("seq")
                if isinstance(seq, bool) or not isinstance(seq, int):
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected integer "
                        f"sequence; actual value is {seq!r}."
                    )
                try:
                    event = EVENT_ADAPTER.validate_python(data)
                except (ValidationError, ValueError) as exc:
                    raise ValueError(
                        f"cassette {source} entry {path} line {number} expected a valid locus "
                        f"event; actual value failed validation: {exc}."
                    ) from exc
                parsed.append((seq, event, number))
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"cassette {source} entry {path} expected UTF-8; actual decode failed: {exc}."
        ) from exc

    seqs = [seq for seq, _, _ in parsed]
    seen: set[int] = set()
    for expected, (actual, _, number) in enumerate(parsed):
        if actual < 0:
            kind = "negative"
        elif actual in seen:
            kind = "duplicated"
        elif expected not in seqs:
            kind = "missing"
        else:
            kind = "out of order"
        if actual != expected:
            raise ValueError(
                f"cassette {source} entry {path} line {number} has a {kind} event sequence; "
                f"expected {expected}, actual {actual}."
            )
        seen.add(actual)

    return [
        StoredEvent(run_id=header.run_id, seq=seq, event=event)
        for seq, event, _ in parsed
    ]


def _canonical_blob_path(root: Path, digest: str) -> Path:
    return root / BLOB_DIR / digest[:2] / digest[2:4] / digest


def _validate_entries(
    source: Path,
    root: Path,
    cassette_path: Path,
    refs: dict[str, set[int]],
    *,
    strict_root: bool,
) -> dict[str, Path]:
    canonical = {digest: _canonical_blob_path(root, digest) for digest in refs}
    expected_files = {path.relative_to(root).as_posix() for path in canonical.values()}
    expected_dirs = {BLOB_DIR} if refs else set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent.as_posix() not in (".", ""):
            expected_dirs.add(parent.as_posix())
            parent = parent.parent

    entries = (
        list(root.rglob("*"))
        if strict_root
        else list((root / BLOB_DIR).rglob("*"))
        if (root / BLOB_DIR).exists()
        else []
    )
    entries.sort(key=lambda entry: entry.is_dir())
    if strict_root:
        expected_files.add(cassette_path.relative_to(root).as_posix())
    for entry in entries:
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            raise ValueError(
                f"cassette {source} expected regular canonical entries; actual entry "
                f"{relative} is a symlink."
            )
        if relative in expected_files:
            if not entry.is_file():
                raise ValueError(
                    f"cassette {source} expected entry {relative} to be a regular file; "
                    "actual entry is not a file."
                )
            continue
        if relative in expected_dirs:
            if not entry.is_dir():
                raise ValueError(
                    f"cassette {source} expected entry {relative} to be a directory; "
                    "actual entry is not a directory."
                )
            continue
        if entry.is_file() and entry.name in refs:
            expected = canonical[entry.name].relative_to(root).as_posix()
            raise ValueError(
                f"cassette {source} has duplicate blob {entry.name}; expected entry "
                f"{expected}, actual extra entry {relative}."
            )
        raise ValueError(
            f"cassette {source} has unexpected entry {relative}; expected only "
            "cassette.jsonl and canonical referenced blob paths."
        )

    for digest, path in canonical.items():
        if path.is_symlink() or not path.is_file():
            relative = path.relative_to(root).as_posix()
            raise ValueError(
                f"cassette {source} references blob {digest}; expected entry {relative}, "
                "actual entry is missing."
            )
    return canonical


def _validate_cassette(source: str | Path) -> _ValidatedCassette:
    path = Path(source)
    if path.is_symlink():
        raise ValueError(
            f"cassette {path} expected a real directory or file; actual source is a symlink."
        )
    strict_root = path.is_dir()
    root = path if strict_root else path.parent
    cassette_path = path / CASSETTE_FILE if strict_root else path
    header = read_header(path)
    if header.format_version != CASSETTE_FORMAT:
        raise ValueError(
            f"cassette {path} entry {cassette_path} expected format version "
            f"{CASSETTE_FORMAT}; actual version {header.format_version}."
        )
    if header.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"cassette {path} entry {cassette_path} expected event schema version "
            f"{SCHEMA_VERSION}; actual version {header.schema_version}. Re-record the run "
            "with a matching locus version."
        )

    events = _read_events(cassette_path, header, path)
    if len(events) != header.event_count:
        raise ValueError(
            f"cassette {path} entry {cassette_path} has the wrong event count; expected "
            f"{header.event_count}, actual {len(events)}."
        )
    actual_digest = run_digest(events)
    if actual_digest != header.digest:
        raise ValueError(
            f"cassette {path} entry {cassette_path} has the wrong logical digest; expected "
            f"{header.digest}, actual {actual_digest}. The event stream was edited or truncated."
        )

    refs = _blob_refs(events)
    blob_paths = _validate_entries(
        path, root, cassette_path, refs, strict_root=strict_root
    )
    blobs: dict[str, bytes] = {}
    for digest, blob_path in blob_paths.items():
        data = blob_path.read_bytes()
        actual_digest = sha256_hex(data)
        relative = blob_path.relative_to(root).as_posix()
        if actual_digest != digest:
            raise ValueError(
                f"cassette {path} blob entry {relative} expected digest {digest}; actual "
                f"digest {actual_digest}. Re-export the cassette."
            )
        actual_size = len(data)
        expected_sizes = refs[digest]
        if expected_sizes != {actual_size}:
            expected = ", ".join(str(size) for size in sorted(expected_sizes))
            raise ValueError(
                f"cassette {path} blob entry {relative} expected size {expected}; actual "
                f"size {actual_size}. Re-export the cassette."
            )
        blobs[digest] = data

    return _ValidatedCassette(path, header, tuple(events), blobs)


def _checked_store_blobs(store: Store, events: list[StoredEvent]) -> dict[str, bytes]:
    checked: dict[str, bytes] = {}
    for digest, sizes in sorted(_blob_refs(events).items()):
        blob_path = store.blobs._path(digest)
        try:
            data = store.blobs.get(digest)
        except KeyError as exc:
            raise KeyError(
                f"stored blob {blob_path} expected digest {digest}; actual file is missing. "
                "Restore the store and blob directory from the same backup before exporting."
            ) from exc
        actual_digest = sha256_hex(data)
        if actual_digest != digest:
            raise ValueError(
                f"stored blob {blob_path} expected digest {digest}; actual digest "
                f"{actual_digest}. Repair or restore the local store before exporting."
            )
        if sizes != {len(data)}:
            expected = ", ".join(str(size) for size in sorted(sizes))
            raise ValueError(
                f"stored blob {blob_path} expected size {expected}; actual size {len(data)}. "
                "Repair or restore the local store before exporting."
            )
        checked[digest] = data
    return checked


def export_cassette(store: Store, run_or_name: str, dest: str | Path) -> Path:
    header = store.resolve(run_or_name)
    events = store.events(header.run_id)
    blob_bytes = _checked_store_blobs(store, events)

    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.locus-export-", dir=out.parent))
    backup: Path | None = None
    try:
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
        lines = [cassette.model_dump_json()] + [_line(event) for event in events]
        (staging / CASSETTE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        for digest, data in blob_bytes.items():
            target = _canonical_blob_path(staging, digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        if out.exists() or out.is_symlink():
            backup = Path(tempfile.mkdtemp(prefix=f".{out.name}.locus-old-", dir=out.parent))
            backup.rmdir()
            out.rename(backup)
        try:
            staging.rename(out)
        except BaseException:
            if backup is not None:
                backup.rename(out)
            raise
        if backup is not None:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return out


def _stage_blobs(store: Store, validated: _ValidatedCassette, stage: Path) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    for digest, data in validated.blobs.items():
        path = _canonical_blob_path(stage, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        staged[digest] = path
    return staged


def _promote_blob(store: Store, digest: str, staged: Path, expected: bytes) -> Path | None:
    target = store.blobs._path(digest)
    if target.exists():
        actual = target.read_bytes()
        actual_digest = sha256_hex(actual)
        if actual_digest != digest or len(actual) != len(expected):
            raise ValueError(
                f"stored blob {target} expected digest {digest} and size {len(expected)}; "
                f"actual digest {actual_digest} and size {len(actual)}. Repair or restore "
                "the local store before importing."
            )
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, target)
    except FileExistsError:
        return _promote_blob(store, digest, staged, expected)
    return target


def import_cassette(source: str | Path, store: Store) -> RunHeader:
    validated = _validate_cassette(source)
    cassette = validated.header
    header = RunHeader(
        run_id=cassette.run_id,
        name=cassette.name,
        started_at=cassette.recorded_at,
        finished_at=cassette.finished_at,
        status=cassette.status,
        schema_version=cassette.schema_version,
        models=cassette.models,
        command=cassette.command,
        redacted=cassette.redacted,
        task_id=cassette.task_id,
    )

    with store._import_staging() as stage:
        staged = _stage_blobs(store, validated, stage)
        created: list[tuple[Path, Path]] = []
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                exists = store._db.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (header.run_id,)
                ).fetchone()
                if exists is not None:
                    raise ValueError(
                        f"run {header.run_id} is already in {store.root}. Importing would "
                        "record it twice; delete it first or import into a different --store."
                    )
                for digest, staged_path in staged.items():
                    promoted = _promote_blob(
                        store, digest, staged_path, validated.blobs[digest]
                    )
                    if promoted is not None:
                        created.append((promoted, staged_path))

                store._insert_run(header)
                for event in validated.events:
                    store._insert_import_event(event)

                stored = store.events(header.run_id)
                actual_digest = run_digest(stored)
                if actual_digest != cassette.digest:
                    raise ValueError(
                        f"importing cassette {validated.source} into {store.root} expected "
                        f"logical digest {cassette.digest}; actual stored digest "
                        f"{actual_digest}. Nothing was imported."
                    )
                _checked_store_blobs(store, stored)
                store._db.commit()
            except BaseException:
                try:
                    for target, staged_path in reversed(created):
                        if target.exists() and os.path.samefile(target, staged_path):
                            target.unlink()
                            store._prune_blob_parents(target)
                finally:
                    store._db.rollback()
                raise
    return header
