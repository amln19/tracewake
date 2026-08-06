from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from .cassette import (
    CASSETTE_FORMAT_VERSION,
    _blob_refs,
    _line,
    _validate_cassette,
)
from .events import EVENT_ADAPTER, EVENT_SCHEMA_VERSION, StoredEvent, run_digest, sha256_hex

BUNDLE_FORMAT_VERSION = 1
MANIFEST_PATH = "manifest.json"
EVENTS_PATH = "events.jsonl"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_ENTRIES = 10_000

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _blob_path(digest: str) -> str:
    return f"blobs/{digest[:2]}/{digest[2:4]}/{digest}"


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleEntry(_ManifestModel):
    path: str
    digest: Digest
    size: int = Field(ge=0)


class BlobEntry(BundleEntry):
    size: int = Field(ge=0, le=MAX_BLOB_BYTES)

    @model_validator(mode="after")
    def _canonical_path(self) -> BlobEntry:
        expected = _blob_path(self.digest)
        if self.path != expected:
            raise ValueError(f"blob {self.digest} path must be {expected}")
        return self


class BundleManifest(_ManifestModel):
    bundle_format_version: int
    cassette_format_version: int
    event_schema_version: int
    run_id: str
    event_count: int = Field(ge=0, le=MAX_EVENTS)
    logical_run_digest: Digest
    events: BundleEntry
    blobs: list[BlobEntry] = Field(max_length=MAX_ENTRIES - 2)


@dataclass(frozen=True)
class ValidatedBundle:
    source: Path
    manifest: BundleManifest
    events: tuple[StoredEvent, ...]
    blobs: dict[str, bytes]
    logical_run_digest: str
    bundle_digest: str
    size: int


def _canonical_json_bytes(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _bundle_bytes(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(entries):
            data = entries[name]
            info = tarfile.TarInfo(name=name)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.size = len(data)
            info.mtime = 0
            info.type = tarfile.REGTYPE
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def build_bundle(cassette: str | Path, destination: str | Path) -> Path:
    validated = _validate_cassette(cassette)
    event_text = "\n".join(_line(event) for event in validated.events)
    event_data = (event_text + ("\n" if event_text else "")).encode("utf-8")
    if len(event_data) > MAX_EVENT_BYTES:
        raise ValueError(
            f"bundle event data is {len(event_data)} bytes; limit is {MAX_EVENT_BYTES}"
        )
    if len(validated.events) > MAX_EVENTS:
        raise ValueError(f"bundle has {len(validated.events)} events; limit is {MAX_EVENTS}")

    blobs = [
        BlobEntry(path=_blob_path(digest), digest=digest, size=len(data))
        for digest, data in sorted(validated.blobs.items())
    ]
    manifest = BundleManifest(
        bundle_format_version=BUNDLE_FORMAT_VERSION,
        cassette_format_version=validated.header.format_version,
        event_schema_version=validated.header.schema_version,
        run_id=validated.header.run_id,
        event_count=len(validated.events),
        logical_run_digest=validated.header.digest,
        events=BundleEntry(
            path=EVENTS_PATH,
            digest=sha256_hex(event_data),
            size=len(event_data),
        ),
        blobs=blobs,
    )
    entries = {MANIFEST_PATH: _canonical_json_bytes(manifest), EVENTS_PATH: event_data}
    entries.update({_blob_path(digest): data for digest, data in validated.blobs.items()})
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"bundle has {len(entries)} entries; limit is {MAX_ENTRIES}")
    raw = _bundle_bytes(entries)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle is {len(raw)} bytes; limit is {MAX_BUNDLE_BYTES}")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and "\\" not in name
        and path.as_posix() == name
        and all(part not in ("", ".", "..") for part in path.parts)
        and len(name.encode("utf-8")) <= 100
    )


def _read_entries(source: Path, raw: bytes) -> dict[str, bytes]:
    if raw.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ")):
        raise ValueError(f"bundle {source} must be an uncompressed USTAR archive")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ENTRIES:
                raise ValueError(
                    f"bundle {source} has {len(members)} entries; limit is {MAX_ENTRIES}"
                )
            names = [member.name for member in members]
            if names != sorted(names):
                raise ValueError(f"bundle {source} entries are not in lexical order")
            if len(names) != len(set(names)):
                raise ValueError(f"bundle {source} contains duplicate entries")
            entries: dict[str, bytes] = {}
            total = 0
            for member in members:
                if not _safe_name(member.name):
                    raise ValueError(
                        f"bundle {source} has noncanonical entry name {member.name!r}"
                    )
                if not member.isfile():
                    raise ValueError(
                        f"bundle {source} entry {member.name} must be a regular file"
                    )
                if member.size > MAX_BUNDLE_BYTES:
                    raise ValueError(
                        f"bundle {source} entry {member.name} exceeds the expanded limit"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"bundle {source} cannot read entry {member.name}")
                data = stream.read(MAX_BUNDLE_BYTES + 1)
                if len(data) != member.size:
                    raise ValueError(
                        f"bundle {source} entry {member.name} declared {member.size} bytes "
                        f"but produced {len(data)}"
                    )
                total += len(data)
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError(f"bundle {source} expanded data exceeds {MAX_BUNDLE_BYTES}")
                entries[member.name] = data
    except (tarfile.ReadError, tarfile.CompressionError) as exc:
        raise ValueError(f"bundle {source} must be an uncompressed USTAR archive: {exc}") from exc
    if raw != _bundle_bytes(entries):
        raise ValueError(
            f"bundle {source} is not canonical uncompressed USTAR with fixed v1 metadata"
        )
    return entries


def _parse_events(source: Path, manifest: BundleManifest, raw: bytes) -> tuple[StoredEvent, ...]:
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError(f"bundle {source} event data exceeds {MAX_EVENT_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"bundle {source} event data must be UTF-8: {exc}") from exc
    if text and not text.endswith("\n"):
        raise ValueError(f"bundle {source} event data must end with LF")
    lines = text.splitlines()
    if len(lines) != manifest.event_count:
        raise ValueError(
            f"bundle {source} expected {manifest.event_count} events; actual {len(lines)}"
        )
    events: list[StoredEvent] = []
    for expected, line in enumerate(lines):
        try:
            data = json.loads(line)
            seq = data.pop("seq")
            event = EVENT_ADAPTER.validate_python(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError, ValueError) as exc:
            raise ValueError(f"bundle {source} event {expected} is invalid: {exc}") from exc
        if seq != expected:
            raise ValueError(
                f"bundle {source} event sequence expected {expected}; actual {seq!r}"
            )
        stored = StoredEvent(run_id=manifest.run_id, seq=seq, event=event)
        if _line(stored) != line:
            raise ValueError(f"bundle {source} event {expected} is not canonical JSON")
        events.append(stored)
    return tuple(events)


def validate_bundle(source: str | Path) -> ValidatedBundle:
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"bundle {path} must be a regular file")
    size = path.stat().st_size
    if size > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle {path} is {size} bytes; limit is {MAX_BUNDLE_BYTES}")
    raw = path.read_bytes()
    entries = _read_entries(path, raw)
    if MANIFEST_PATH not in entries or EVENTS_PATH not in entries:
        raise ValueError(f"bundle {path} must contain {MANIFEST_PATH} and {EVENTS_PATH}")
    try:
        manifest = BundleManifest.model_validate_json(entries[MANIFEST_PATH])
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"bundle {path} manifest is invalid: {exc}") from exc
    if entries[MANIFEST_PATH] != _canonical_json_bytes(manifest):
        raise ValueError(f"bundle {path} manifest is not canonical JSON")
    if manifest.bundle_format_version != BUNDLE_FORMAT_VERSION:
        raise ValueError(
            f"bundle {path} uses bundle format {manifest.bundle_format_version}; "
            f"supported version is {BUNDLE_FORMAT_VERSION}"
        )
    if manifest.cassette_format_version != CASSETTE_FORMAT_VERSION:
        raise ValueError(
            f"bundle {path} uses cassette format {manifest.cassette_format_version}; "
            f"supported version is {CASSETTE_FORMAT_VERSION}"
        )
    if manifest.event_schema_version != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"bundle {path} uses event schema {manifest.event_schema_version}; "
            f"supported version is {EVENT_SCHEMA_VERSION}"
        )

    event_data = entries[EVENTS_PATH]
    if manifest.events.path != EVENTS_PATH:
        raise ValueError(f"bundle {path} manifest event path must be {EVENTS_PATH}")
    if manifest.events.size != len(event_data) or manifest.events.digest != sha256_hex(event_data):
        raise ValueError(f"bundle {path} event digest or size does not match the manifest")
    events = _parse_events(path, manifest, event_data)
    logical = run_digest(events)
    if logical != manifest.logical_run_digest:
        raise ValueError(
            f"bundle {path} expected logical digest {manifest.logical_run_digest}; actual {logical}"
        )

    refs = _blob_refs(events)
    declared = {blob.digest: blob for blob in manifest.blobs}
    if len(declared) != len(manifest.blobs):
        raise ValueError(f"bundle {path} manifest contains duplicate blob declarations")
    if set(declared) != set(refs):
        raise ValueError(f"bundle {path} manifest blob declarations do not match event references")
    expected_entries = {MANIFEST_PATH, EVENTS_PATH, *(blob.path for blob in manifest.blobs)}
    if set(entries) != expected_entries:
        raise ValueError(f"bundle {path} contains missing or unexpected entries")

    blobs: dict[str, bytes] = {}
    for digest, declaration in declared.items():
        data = entries[declaration.path]
        if len(data) != declaration.size or sha256_hex(data) != digest:
            raise ValueError(
                f"bundle {path} blob {declaration.path} digest or size does not match"
            )
        if refs[digest] != {len(data)}:
            raise ValueError(
                f"bundle {path} blob {declaration.path} size disagrees with event references"
            )
        blobs[digest] = data

    return ValidatedBundle(
        source=path,
        manifest=manifest,
        events=events,
        blobs=blobs,
        logical_run_digest=logical,
        bundle_digest=sha256_hex(raw),
        size=len(raw),
    )
