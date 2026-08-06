"""Documented constraints the rest of the suite can miss by holding the
interesting variable constant.

Each test names one invariant. Failures here are silent data loss, silent
corruption of a recording, or a cassette that can read files outside its store.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import locus
from locus import (
    DecodeParams,
    Message,
    ModelResponse,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)
from locus.cassette import _blob_digests, export_cassette, import_cassette
from locus.cli import _restore_command
from locus.config import Config
from locus.events import BlobRef, EVENT_ADAPTER
from locus.redaction import HOME_PLACEHOLDER, Redactor


def _create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
    return ModelResponse(
        text="hi",
        tool_calls=[ToolCallRequest(id="t0", name="read", args={"path": "a.py"}, batch_index=0)],
        finish_reason="tool_use",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def test_export_ships_every_blob_an_event_references(tmp_path: Path) -> None:
    """No cassette may drop a BlobRef: tool results, fs content, and outcome patches."""
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    note = tmp_path / "note.txt"
    with locus.record("full", store=tmp_path / "src") as rec:
        call = rec.model(provider="p", model_id="m", create_fn=_create).create(
            messages=[Message(role="user", content="go")]
        )
        rec.tools(lambda n, a: ToolOutcome(content="tool body")).call(
            call.call_id, call.response.tool_calls[0]
        )
        rec.fs.write_text(note, "file body")
        rec.outcome(status="ok", resolve=True, coverage=True, patch=patch)
        run_id = rec.run_id

    db = Store(tmp_path / "src")
    events = db.events(run_id)
    needed = _blob_digests(events)
    assert len(needed) >= 3, "the run must exercise more than one blob-bearing field"

    cass = tmp_path / "cassette"
    export_cassette(db, run_id, cass)
    shipped = {p.name for p in (cass / "blobs").rglob("*") if p.is_file()}
    assert needed <= shipped

    into = Store(tmp_path / "dst")
    import_cassette(cass, into)
    for digest in needed:
        assert into.blobs.has(digest)
        assert into.blobs.get(digest) == db.blobs.get(digest)
    into.close()
    db.close()


def test_export_fails_when_a_referenced_blob_is_missing_from_the_store(
    tmp_path: Path,
) -> None:
    """A future event type with a BlobRef must not export as success with an empty blobs/."""
    with locus.record("gap", store=tmp_path / "src") as rec:
        rec.outcome(
            status="ok",
            resolve=True,
            coverage=True,
            patch="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        )
        run_id = rec.run_id

    db = Store(tmp_path / "src")
    (outcome,) = [e.event for e in db.events(run_id) if e.event.type == "outcome"]
    assert outcome.patch is not None
    digest = outcome.patch.digest
    path = db.blobs._path(digest)
    path.unlink()

    dest = tmp_path / "cassette"
    with pytest.raises(KeyError, match="missing"):
        export_cassette(db, run_id, dest)
    assert not dest.exists(), "a failed export must not leave a partial cassette"
    db.close()


def test_finish_if_running_leaves_a_finished_cassette_alone(tmp_path: Path) -> None:
    """A missing child report must not rewrite an already-finished once/none cassette."""
    from locus.cli import _finish_if_running

    with locus.session("demo", store=tmp_path, mode="once") as rec:
        rec.clock.time()
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(tmp_path)
    before = db.run(run_id)
    _finish_if_running(db, run_id, code=1)
    after = db.run(run_id)
    db.close()
    assert after.status == before.status == "ok"
    assert after.finished_at == before.finished_at


def test_a_pure_once_replay_does_not_rewrite_the_recording(tmp_path: Path) -> None:
    """`--mode once` against an existing cassette is replay-only: status and finished_at stay."""
    store = tmp_path / "store"
    marker = tmp_path / "diverge"
    script = tmp_path / "agent.py"
    script.write_text(
        "import os, locus\n"
        f"marker = {str(marker)!r}\n"
        "s = locus.current()\n"
        "s.clock.time()\n"
        "if os.path.exists(marker):\n"
        "    s.clock.time()\n"
        "s.outcome(status='ok')\n",
        encoding="utf-8",
    )

    first = subprocess.run(
        [
            sys.executable,
            "-m",
            "locus",
            "record",
            "--store",
            str(store),
            "--name",
            "demo",
            "--mode",
            "once",
            "--",
            sys.executable,
            str(script),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    db = Store(store)
    recorded = db.latest_named("demo")
    assert recorded.status == "ok"
    finished_at = recorded.finished_at
    run_id = recorded.run_id
    db.close()

    marker.write_text("x", encoding="utf-8")
    second = subprocess.run(
        [
            sys.executable,
            "-m",
            "locus",
            "record",
            "--store",
            str(store),
            "--name",
            "demo",
            "--mode",
            "once",
            "--",
            sys.executable,
            str(script),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode != 0

    db = Store(store)
    after = db.run(run_id)
    db.close()
    assert after.status == "ok"
    assert after.finished_at == finished_at


def test_library_once_replay_reports_can_record_false_and_leaves_the_run(
    tmp_path: Path,
) -> None:
    with locus.session("greet", store=tmp_path, mode="once") as first:
        first.clock.time()
        first.outcome(status="ok")
        run_id = first.run_id
        assert first.can_record is True
        assert first.report.can_record is True

    db = Store(tmp_path)
    before = db.run(run_id)
    db.close()

    with locus.session("greet", store=tmp_path, mode="once") as second:
        assert second.run_id == run_id
        assert second.can_record is False
        assert second.report.can_record is False
        second.clock.time()
        second.outcome(status="ok")

    db = Store(tmp_path)
    after = db.run(run_id)
    db.close()
    assert after.status == before.status
    assert after.finished_at == before.finished_at


def test_blob_digests_are_rejected_before_they_become_paths(tmp_path: Path) -> None:
    """A cassette cannot smuggle a filesystem path through BlobRef.digest."""
    with pytest.raises(ValidationError):
        BlobRef(digest="../../../etc/passwd", size=1)
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            {
                "type": "tool_call",
                "tool_call_id": "t0",
                "batch_index": 0,
                "name": "read",
                "args": {},
                "args_hash": "0" * 64,
                "result": {"digest": str(tmp_path / "secret"), "size": 1},
                "status": "ok",
                "meta": {"recorded_at": 1.0},
            }
        )

    store = Store(tmp_path / "store")
    with pytest.raises(ValueError, match="64-character lowercase hex"):
        store.blobs.get("../../secret")
    store.close()


def test_import_refuses_a_cassette_with_a_path_shaped_digest(tmp_path: Path) -> None:
    victim = tmp_path / "id_rsa"
    victim.write_text("HARVESTED\n", encoding="utf-8")

    with locus.record("seed", store=tmp_path / "src") as rec:
        call = rec.model(provider="p", model_id="m", create_fn=_create).create(
            messages=[Message(role="user", content="go")]
        )
        rec.tools(lambda n, a: ToolOutcome(content="ok")).call(
            call.call_id, call.response.tool_calls[0]
        )
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(tmp_path / "src")
    export_cassette(db, run_id, tmp_path / "cassette")
    db.close()

    path = tmp_path / "cassette" / "cassette.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines[1:]]
    for event in events:
        if event.get("type") == "tool_call":
            event["result"]["digest"] = "../../../.." + str(victim)
            event["result"]["size"] = victim.stat().st_size
    path.write_text(
        lines[0] + "\n" + "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    into = Store(tmp_path / "dst")
    with pytest.raises(ValueError, match="not a valid locus event|String should match"):
        import_cassette(tmp_path / "cassette", into)
    assert into.runs() == []
    into.close()


def test_header_command_is_scrubbed_in_the_store_and_the_exported_cassette(
    tmp_path: Path,
) -> None:
    """No cassette may be written with credentials or absolute home paths in argv."""
    home = str(Path.home())
    secret = "sk-live-9d2f4a7b1c8e0356"
    command = ["python", f"{home}/work/agent.py", "--api-key", secret]

    with locus._open_session(
        "demo",
        store=tmp_path / "src",
        mode="all",
        command=command,
        filter_values=(secret,),
    ) as s:
        s.outcome(status="ok")
        run_id = s.run_id

    db = Store(tmp_path / "src")
    header = db.run(run_id)
    assert home not in "".join(header.command or [])
    assert secret not in "".join(header.command or [])
    assert HOME_PLACEHOLDER in "".join(header.command or [])
    assert locus.REDACTED in (header.command or [])

    cass = tmp_path / "cassette"
    export_cassette(db, run_id, cass)
    db.close()
    text = (cass / "cassette.jsonl").read_text(encoding="utf-8")
    assert home not in text
    assert secret not in text


def test_open_session_is_not_part_of_the_public_python_surface() -> None:
    assert "open_session" not in locus.__dict__
    assert "open_session" not in locus.__all__


def test_replay_restores_home_paths_in_the_recorded_command() -> None:
    home = str(Path.home())
    scrubbed = [
        "python",
        f"{HOME_PLACEHOLDER}/work/agent.py",
        "--api-key",
        locus.REDACTED,
    ]
    restored = _restore_command(scrubbed, redacted=True)
    assert restored[1] == f"{home}/work/agent.py"
    assert restored[3] == locus.REDACTED

    redactor = Redactor(Config(redact=True))
    assert redactor.restore_path(f"{HOME_PLACEHOLDER}/x") == f"{home}/x"
