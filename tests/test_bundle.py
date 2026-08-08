from __future__ import annotations

import gzip
from pathlib import Path

import pytest

import tracewake
from tracewake.bundle import (
    BUNDLE_FORMAT_VERSION,
    MAX_BLOB_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_ENTRIES,
    MAX_EVENTS,
    build_bundle,
    validate_bundle,
)
from tracewake.cassette import export_cassette
from tracewake.cassette import CassetteHeader
from tracewake.events import EVENT_SCHEMA_VERSION, run_digest, sha256_hex
from tracewake.store import Store


def _cassette(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "store"
    with tracewake.record("bundle-fixture", store=source) as rec:
        rec.outcome(status="ok", patch="fixed patch bytes")
        run_id = rec.run_id
    store = Store(source)
    try:
        cassette = export_cassette(store, run_id, tmp_path / "cassette")
    finally:
        store.close()
    return cassette, run_id


def test_bundle_v1_limits_are_frozen() -> None:
    assert BUNDLE_FORMAT_VERSION == 1
    assert MAX_BUNDLE_BYTES == 256 * 1024 * 1024
    assert MAX_BLOB_BYTES == 64 * 1024 * 1024
    assert MAX_EVENTS == 100_000
    assert MAX_ENTRIES == 10_000


def test_supported_platform_production_is_byte_identical(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)

    first = build_bundle(cassette, tmp_path / "first.tracewake")
    second = build_bundle(cassette, tmp_path / "second.tracewake")

    assert first.read_bytes() == second.read_bytes()
    assert sha256_hex(first.read_bytes()) == validate_bundle(first).bundle_digest


def test_bundle_round_trip_validates_events_and_blobs(tmp_path: Path) -> None:
    cassette, run_id = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.tracewake")

    validated = validate_bundle(bundle)

    assert validated.manifest.run_id == run_id
    assert validated.manifest.logical_run_digest == validated.logical_run_digest
    assert validated.manifest.event_count == len(validated.events)
    assert list(validated.blobs.values()) == [b"fixed patch bytes"]


def test_bundle_rejects_changed_archive_bytes(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.tracewake")
    changed = bytearray(bundle.read_bytes())
    changed[0] = ord("x")
    bundle.write_bytes(changed)

    with pytest.raises(ValueError, match="bundle|archive|canonical"):
        validate_bundle(bundle)


def test_bundle_rejects_compression(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.tracewake")
    compressed = tmp_path / "compressed.tracewake"
    compressed.write_bytes(gzip.compress(bundle.read_bytes(), mtime=0))

    with pytest.raises(ValueError, match="uncompressed USTAR"):
        validate_bundle(compressed)


def test_bundle_validation_is_write_free(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.tracewake")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    validate_bundle(bundle)

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_bundle_supports_an_empty_event_stream(tmp_path: Path) -> None:
    cassette = tmp_path / "empty"
    cassette.mkdir()
    header = CassetteHeader(
        tracewake_version="0.2.0",
        schema_version=EVENT_SCHEMA_VERSION,
        run_id="empty-run",
        name="empty",
        recorded_at=1_700_000_000.0,
        status="running",
        models=[],
        event_count=0,
        digest=run_digest([]),
    )
    (cassette / "cassette.jsonl").write_text(
        header.model_dump_json() + "\n", encoding="utf-8"
    )

    validated = validate_bundle(build_bundle(cassette, tmp_path / "empty.tracewake"))

    assert validated.events == ()
    assert validated.manifest.events.size == 0
