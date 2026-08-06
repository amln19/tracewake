from __future__ import annotations

import json
import os
import shutil
import time
import warnings
from pathlib import Path
from typing import Any, Callable

import pytest

import locus
from locus import (
    DecodeParams,
    Message,
    ModelResponse,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
    run_digest,
)
from locus.cassette import (
    CASSETTE_FORMAT,
    CASSETTE_FORMAT_VERSION,
    _validate_cassette,
    export_cassette,
    import_cassette,
    read_header,
)
from locus.events import EVENT_ADAPTER, EVENT_SCHEMA_VERSION, StoredEvent, sha256_hex


def _create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
    return ModelResponse(text="hello", finish_reason="end_turn", usage=Usage(input_tokens=3))


def _record(store: Path, name: str = "trip") -> str:
    with locus.record(name, store=store) as rec:
        model = rec.model(
            provider="acme", model_id="acme-1", model_version="2026-05-01", create_fn=_create
        )
        completion = model.create(messages=[Message(role="user", content="hi")])
        rec.tools(lambda n, a: ToolOutcome(content="tool output")).call(
            completion.call_id,
            ToolCallRequest(id="t0", name="read", args={"path": "a.py"}, batch_index=0),
        )
        rec.clock.time()
        rec.outcome(status="ok", usage=Usage(input_tokens=3), patch="recorded patch")
        return rec.run_id


def _exported(tmp_path: Path) -> tuple[Path, str]:
    run_id = _record(tmp_path / "source")
    db = Store(tmp_path / "source")
    cassette = export_cassette(db, run_id, tmp_path / "cassette")
    db.close()
    return cassette, run_id


def _rewrite(
    cassette: Path,
    change: Callable[[dict[str, Any], list[dict[str, Any]]], None],
) -> None:
    path = cassette / "cassette.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    events = [json.loads(line) for line in lines[1:]]
    change(header, events)
    path.write_text(
        "\n".join(json.dumps(value) for value in [header, *events]) + "\n",
        encoding="utf-8",
    )


def _blob_paths(cassette: Path) -> list[Path]:
    return [path for path in (cassette / "blobs").rglob("*") if path.is_file()]


def test_a_cassette_round_trips_without_changing_the_run(tmp_path: Path) -> None:
    source, target = tmp_path / "a", tmp_path / "b"
    run_id = _record(source)

    db = Store(source)
    original = run_digest(db.events(run_id))
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    into = Store(target)
    header = import_cassette(tmp_path / "cassette", into)
    assert header.run_id == run_id
    assert run_digest(into.events(run_id)) == original
    into.close()


def test_a_cassette_carries_the_task_it_belongs_to(tmp_path: Path) -> None:
    source, target = tmp_path / "a", tmp_path / "b"
    with locus.record("trip", store=source, task_id="toolz-guard-2") as rec:
        rec.outcome(status="ok", coverage=True, resolve=False)
        run_id = rec.run_id

    db = Store(source)
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()
    assert read_header(tmp_path / "cassette").task_id == "toolz-guard-2"

    into = Store(target)
    assert import_cassette(tmp_path / "cassette", into).task_id == "toolz-guard-2"
    (outcome,) = [e.event for e in into.events(run_id) if e.event.type == "outcome"]
    assert (outcome.coverage, outcome.resolve) == (True, False)
    into.close()


def test_an_imported_cassette_replays(tmp_path: Path) -> None:
    source, target = tmp_path / "a", tmp_path / "b"
    run_id = _record(source)
    db = Store(source)
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    into = Store(target)
    import_cassette(tmp_path / "cassette", into)
    into.close()

    with locus.replay(run_id, store=target) as rep:
        model = rep.model(provider="acme", model_id="acme-1")
        assert model.create(messages=[Message(role="user", content="hi")]).response.text == "hello"
        rep.outcome(status="ok")


def test_the_header_says_what_produced_the_cassette(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    header = read_header(tmp_path / "cassette")
    assert header.name == "trip"
    assert header.recorded_at > 1_700_000_000
    assert [(m.provider, m.model_id, m.model_version) for m in header.models] == [
        ("acme", "acme-1", "2026-05-01")
    ]
    assert header.status == "ok"
    assert header.format_version == CASSETTE_FORMAT_VERSION
    assert header.schema_version == EVENT_SCHEMA_VERSION


def test_the_cassette_format_alias_remains_compatible() -> None:
    assert CASSETTE_FORMAT == CASSETTE_FORMAT_VERSION


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("format_version", "expected format version"),
        ("schema_version", "event schema version"),
    ],
)
def test_cassette_and_event_versions_are_rejected_independently(
    tmp_path: Path, field: str, message: str
) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        header[field] = 999

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=message):
        _validate_cassette(cassette)


def test_the_cassette_is_line_oriented_and_readable(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    lines = (tmp_path / "cassette" / "cassette.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1 + len(Store(tmp_path / "a").events(run_id))
    assert json.loads(lines[1])["type"] == "model_call"
    assert all(json.loads(line) for line in lines)


def test_an_edited_cassette_is_refused(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    path = tmp_path / "cassette" / "cassette.jsonl"
    lines = path.read_text().strip().split("\n")
    event = json.loads(lines[1])
    event["response"]["text"] = "tampered"
    lines[1] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n")

    into = Store(tmp_path / "b")
    with pytest.raises(ValueError, match="wrong logical digest"):
        import_cassette(tmp_path / "cassette", into)
    # Refused before anything was written. A half-imported run would make the
    # retry fail with "already in the store" about a run that never imported.
    assert into.runs() == []
    into.close()


def test_importing_the_same_run_twice_is_refused(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    into = Store(tmp_path / "b")
    import_cassette(tmp_path / "cassette", into)
    with pytest.raises(ValueError, match="already in"):
        import_cassette(tmp_path / "cassette", into)
    into.close()


def test_blobs_travel_with_the_cassette(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    blobs = list((tmp_path / "cassette" / "blobs").rglob("*"))
    assert any(b.is_file() and b.read_bytes() == b"tool output" for b in blobs)


def test_an_outcome_patch_blob_travels_with_the_cassette(tmp_path: Path) -> None:
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    source, target = tmp_path / "a", tmp_path / "b"
    with locus.record("patched", store=source) as rec:
        rec.outcome(status="ok", resolve=True, coverage=True, patch=patch)
        run_id = rec.run_id

    db = Store(source)
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    into = Store(target)
    import_cassette(tmp_path / "cassette", into)
    (outcome,) = [e.event for e in into.events(run_id) if e.event.type == "outcome"]
    assert into.blobs.get(outcome.patch.digest) == patch.encode()
    into.close()


def test_import_refuses_a_cassette_whose_blob_is_missing(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    for blob in (tmp_path / "cassette" / "blobs").rglob("*"):
        if blob.is_file():
            blob.unlink()

    into = Store(tmp_path / "b")
    with pytest.raises(ValueError, match="references blob"):
        import_cassette(tmp_path / "cassette", into)
    assert into.runs() == []
    into.close()


def test_validation_hashes_blob_bytes_instead_of_trusting_the_filename(
    tmp_path: Path,
) -> None:
    cassette, _ = _exported(tmp_path)
    blob = _blob_paths(cassette)[0]
    blob.write_bytes(b"x" * blob.stat().st_size)

    with pytest.raises(ValueError, match=r"blob.*expected digest.*actual digest"):
        _validate_cassette(cassette)


def test_validation_checks_blob_size_against_every_reference(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        for event in events:
            if event["type"] == "tool_call":
                event["result"]["size"] += 1
        stored = []
        for event in events:
            data = dict(event)
            seq = data.pop("seq")
            stored.append(
                StoredEvent(
                    run_id=header["run_id"],
                    seq=seq,
                    event=EVENT_ADAPTER.validate_python(data),
                )
            )
        header["digest"] = run_digest(stored)

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=r"expected size.*actual size"):
        _validate_cassette(cassette)


def test_validation_rejects_a_negative_blob_size(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        for event in events:
            if event["type"] == "tool_call":
                event["result"]["size"] = -1

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=r"cassette.jsonl line.*valid locus event"):
        _validate_cassette(cassette)


def test_validation_rejects_an_unexpected_blob_path(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)
    unexpected = cassette / "blobs" / "unexpected"
    unexpected.write_bytes(b"not referenced")

    with pytest.raises(ValueError, match=r"unexpected entry.*blobs/unexpected"):
        _validate_cassette(cassette)


def test_validation_rejects_a_duplicate_noncanonical_blob_path(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)
    blob = _blob_paths(cassette)[0]
    duplicate = cassette / "blobs" / "ff" / "ff" / blob.name
    duplicate.parent.mkdir(parents=True)
    shutil.copyfile(blob, duplicate)

    with pytest.raises(ValueError, match=r"duplicate blob.*blobs/ff/ff"):
        _validate_cassette(cassette)


def test_validation_rejects_symlinks(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)
    blob = _blob_paths(cassette)[0]
    link = cassette / "blobs" / "linked"
    os.symlink(blob, link)

    with pytest.raises(ValueError, match=r"blobs/linked.*symlink"):
        _validate_cassette(cassette)


def test_validation_checks_the_header_event_count(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        header["event_count"] += 1

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=r"event count.*expected.*actual"):
        _validate_cassette(cassette)


@pytest.mark.parametrize(
    ("seqs", "kind"),
    [
        ([-1, 1], "negative"),
        ([0, 0], "duplicated"),
        ([0, 2], "missing"),
        ([1, 0], "out of order"),
    ],
)
def test_validation_requires_dense_ordered_event_sequences(
    tmp_path: Path, seqs: list[int], kind: str
) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        for event, seq in zip(events, seqs, strict=False):
            event["seq"] = seq

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=rf"{kind}.*sequence.*expected.*actual"):
        _validate_cassette(cassette)


def test_validation_checks_the_logical_run_digest(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)

    def change(header: dict[str, Any], events: list[dict[str, Any]]) -> None:
        header["digest"] = "0" * 64

    _rewrite(cassette, change)
    with pytest.raises(ValueError, match=r"logical digest.*expected.*actual"):
        _validate_cassette(cassette)


def test_validation_failure_does_not_change_store_state(tmp_path: Path) -> None:
    cassette, _ = _exported(tmp_path)
    _blob_paths(cassette)[0].unlink()
    target = Store(tmp_path / "target")
    target.blobs.put(b"already here")
    before_runs = target.runs()
    before_blobs = sorted(path.relative_to(target.root) for path in target.root.rglob("*") if path.is_file())

    with pytest.raises(ValueError):
        import_cassette(cassette, target)

    after_blobs = sorted(path.relative_to(target.root) for path in target.root.rglob("*") if path.is_file())
    assert target.runs() == before_runs
    assert after_blobs == before_blobs
    target.close()


def test_export_refuses_corrupted_stored_blob_bytes_and_preserves_destination(
    tmp_path: Path,
) -> None:
    run_id = _record(tmp_path / "source")
    db = Store(tmp_path / "source")
    first = export_cassette(db, run_id, tmp_path / "cassette")
    marker = first / "keep.txt"
    marker.write_text("old export", encoding="utf-8")
    event = next(stored.event for stored in db.events(run_id) if stored.event.type == "tool_call")
    stored = db.blobs._path(event.result.digest)
    stored.write_bytes(b"x" * event.result.size)

    with pytest.raises(ValueError, match=r"(?i)stored blob.*expected digest.*repair"):
        export_cassette(db, run_id, tmp_path / "cassette")

    assert marker.read_text(encoding="utf-8") == "old export"
    db.close()


def test_handled_import_failure_removes_only_new_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cassette, run_id = _exported(tmp_path)
    target = Store(tmp_path / "target")
    source_blobs = {path.name: path.read_bytes() for path in _blob_paths(cassette)}
    preserved_digest, preserved_bytes = next(iter(source_blobs.items()))
    target.blobs.put(preserved_bytes)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(target, "_insert_import_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        import_cassette(cassette, target)

    assert target.find(run_id) is None
    assert target.blobs.get(preserved_digest) == preserved_bytes
    for digest in source_blobs.keys() - {preserved_digest}:
        assert not target.blobs.has(digest)
    target.close()


def test_store_startup_cleans_blob_links_from_an_abandoned_import(tmp_path: Path) -> None:
    root = tmp_path / "target"
    store = Store(root)
    data = b"orphaned import bytes"
    digest = sha256_hex(data)
    stage = root / ".imports" / "import-crashed"
    staged = stage / "blobs" / digest[:2] / digest[2:4] / digest
    staged.parent.mkdir(parents=True)
    staged.write_bytes(data)
    (stage / ".lock").touch()
    target = store.blobs._path(digest)
    target.parent.mkdir(parents=True)
    os.link(staged, target)
    store.close()

    recovered = Store(root)
    assert not target.exists()
    assert not stage.exists()
    recovered.close()


def test_an_old_cassette_warns_before_it_replays(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    db.finish_run(run_id, "ok", time.time())
    db._db.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?",
        (time.time() - 200 * 86400, run_id),
    )
    db._db.commit()
    db.close()

    with pytest.warns(locus.CassetteStale, match="200 days ago"):
        with locus.replay(run_id, store=tmp_path / "a") as rep:
            rep.model(provider="acme", model_id="acme-1")


def test_the_staleness_warning_names_the_model_that_may_have_moved(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    db = Store(tmp_path / "a")
    db._db.execute(
        "UPDATE runs SET started_at = ? WHERE run_id = ?",
        (time.time() - 400 * 86400, run_id),
    )
    db._db.commit()
    db.close()

    with pytest.warns(locus.CassetteStale, match="acme/acme-1"):
        with locus.replay(run_id, store=tmp_path / "a"):
            pass


def test_a_fresh_cassette_does_not_warn(tmp_path: Path) -> None:
    run_id = _record(tmp_path / "a")
    with warnings.catch_warnings():
        warnings.simplefilter("error", locus.CassetteStale)
        with locus.replay(run_id, store=tmp_path / "a"):
            pass
