from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

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
from locus.cassette import export_cassette, import_cassette, read_header


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
        rec.outcome(status="ok", usage=Usage(input_tokens=3))
        return rec.run_id


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
    with pytest.raises(ValueError, match="did not survive the round trip"):
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
