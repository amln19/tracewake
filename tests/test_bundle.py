from __future__ import annotations

import gzip
from pathlib import Path

import pytest

import locus
from locus.bundle import (
    BUNDLE_FORMAT_VERSION,
    MAX_BLOB_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_ENTRIES,
    MAX_EVENTS,
    build_bundle,
    validate_bundle,
)
from locus.cassette import export_cassette
from locus.events import sha256_hex
from locus.store import Store


def _cassette(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "store"
    with locus.record("bundle-fixture", store=source) as rec:
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

    first = build_bundle(cassette, tmp_path / "first.locus")
    second = build_bundle(cassette, tmp_path / "second.locus")

    assert first.read_bytes() == second.read_bytes()
    assert sha256_hex(first.read_bytes()) == validate_bundle(first).bundle_digest


def test_bundle_round_trip_validates_events_and_blobs(tmp_path: Path) -> None:
    cassette, run_id = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.locus")

    validated = validate_bundle(bundle)

    assert validated.manifest.run_id == run_id
    assert validated.manifest.logical_run_digest == validated.logical_run_digest
    assert validated.manifest.event_count == len(validated.events)
    assert list(validated.blobs.values()) == [b"fixed patch bytes"]


def test_bundle_rejects_changed_archive_bytes(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.locus")
    changed = bytearray(bundle.read_bytes())
    changed[0] = ord("x")
    bundle.write_bytes(changed)

    with pytest.raises(ValueError, match="bundle|archive|canonical"):
        validate_bundle(bundle)


def test_bundle_rejects_compression(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.locus")
    compressed = tmp_path / "compressed.locus"
    compressed.write_bytes(gzip.compress(bundle.read_bytes(), mtime=0))

    with pytest.raises(ValueError, match="uncompressed USTAR"):
        validate_bundle(compressed)


def test_bundle_validation_is_write_free(tmp_path: Path) -> None:
    cassette, _ = _cassette(tmp_path)
    bundle = build_bundle(cassette, tmp_path / "run.locus")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    validate_bundle(bundle)

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
