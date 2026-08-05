from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import locus
from locus import EnvironmentEvent, EventMeta, RunHeader, Store


def _run(store: Store, run_id: str = "r1") -> str:
    store.create_run(
        RunHeader(run_id=run_id, name="t", started_at=time.time(), status="running")
    )
    return run_id


def test_database_is_in_wal_mode(tmp_path: Path) -> None:
    store = Store(tmp_path)
    (mode,) = store._db.execute("PRAGMA journal_mode").fetchone()
    assert mode == "wal"
    store.close()


def test_blobs_are_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = Store(tmp_path)
    a = store.blobs.put(b"same contents")
    b = store.blobs.put(b"same contents")
    c = store.blobs.put(b"different")

    assert a == b
    assert a.digest != c.digest
    assert a.size == len(b"same contents")
    assert store.blobs.get(a.digest) == b"same contents"
    assert len(list((tmp_path / "blobs").rglob("*"))) == len({a.digest, c.digest}) * 3
    store.close()


def test_missing_blob_says_what_to_do(tmp_path: Path) -> None:
    store = Store(tmp_path)
    with pytest.raises(KeyError, match="copied together"):
        store.blobs.get("0" * 64)
    store.close()


def test_a_path_shaped_digest_is_refused(tmp_path: Path) -> None:
    store = Store(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    evil = "../../../.." + str(secret)
    with pytest.raises(ValueError, match="64-character lowercase hex"):
        store.blobs.has(evil)
    with pytest.raises(ValueError, match="64-character lowercase hex"):
        store.blobs.get(evil)
    store.close()


def test_blob_ref_rejects_a_non_digest_string() -> None:
    from pydantic import ValidationError

    from locus.events import BlobRef

    with pytest.raises(ValidationError):
        BlobRef(digest="../../../etc/passwd", size=1)
    with pytest.raises(ValidationError):
        BlobRef(digest="abcd", size=1)


def test_sequence_numbers_are_dense_under_concurrent_appends(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run_id = _run(store)

    def append(i: int) -> int:
        return store.append(
            run_id,
            EnvironmentEvent(source="clock", value=float(i), meta=EventMeta(recorded_at=0.0)),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = sorted(pool.map(append, range(200)))

    assert seqs == list(range(200))
    assert [e.seq for e in store.events(run_id)] == list(range(200))
    store.close()


def test_events_round_trip_through_canonical_and_meta_columns(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run_id = _run(store)
    original = EnvironmentEvent(
        source="clock", value=1753718400.123456, meta=EventMeta(recorded_at=1.5, duration_ms=2.5)
    )
    store.append(run_id, original)

    (stored,) = store.events(run_id)
    assert stored.event == original
    assert stored.event.value == 1753718400.123456
    assert b"recorded_at" not in stored.event.canonical_bytes()
    store.close()


def test_runs_group_by_task_in_start_order(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for index, (run_id, task_id) in enumerate(
        [("a", "t1"), ("b", "t2"), ("c", "t1"), ("d", None)]
    ):
        store.create_run(
            RunHeader(
                run_id=run_id,
                name=run_id,
                started_at=1000.0 + index,
                status="ok",
                task_id=task_id,
            )
        )

    assert [h.run_id for h in store.runs("t1")] == ["a", "c"]
    assert store.tasks() == ["t1", "t2"]
    assert len(store.runs()) == 4
    store.close()


def test_outcome_labels_survive_the_store_round_trip(tmp_path: Path) -> None:
    with locus.record("labelled", store=tmp_path, task_id="slugify-op-swap-1") as rec:
        rec.outcome(
            status="ok",
            coverage=True,
            resolve=False,
            patch="--- a/x.py\n+++ b/x.py\n",
            test_summary="1 failed, 40 passed",
        )
        run_id = rec.run_id

    store = Store(tmp_path)
    assert store.run(run_id).task_id == "slugify-op-swap-1"
    (outcome,) = [e.event for e in store.events(run_id) if e.event.type == "outcome"]
    assert (outcome.coverage, outcome.resolve) == (True, False)
    assert outcome.test_summary == "1 failed, 40 passed"
    assert store.blobs.get(outcome.patch.digest).decode() == "--- a/x.py\n+++ b/x.py\n"
    store.close()


def test_a_store_written_in_an_older_format_says_so(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store._db.execute("PRAGMA user_version = 2")
    store._db.commit()
    store.close()

    with pytest.raises(ValueError, match="format 2"):
        Store(tmp_path)


def test_an_id_prefix_is_matched_literally_not_as_a_pattern(tmp_path: Path) -> None:
    """`_` and `%` are SQL LIKE wildcards; an abbreviated run id is neither."""
    store = Store(tmp_path)
    _run(store, "abc123def456")

    assert store.find("abc").run_id == "abc123def456"
    # Both of these match "abc123def456" if the prefix is read as a pattern.
    assert store.find("a_c") is None
    assert store.find("%") is None
    store.close()


def test_unknown_run_lists_the_runs_that_exist(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _run(store, "known")
    with pytest.raises(KeyError, match="known"):
        store.run("unknown")
    store.close()
