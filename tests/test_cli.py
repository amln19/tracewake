from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import locus
from locus import DecodeParams, Message, ModelResponse, Store, Usage

SCRIPT = """\
import locus
from locus import Message, ModelResponse, Usage


def create(model_id, messages, params):
    return ModelResponse(text="from the script", finish_reason="end_turn", usage=Usage())


with locus.record("ignored-under-the-wrapper") as rec:
    model = rec.model(provider="p", model_id="m", create_fn=create)
    print(model.create(messages=[Message(role="user", content="hi")]).response.text)
    rec.outcome(status="ok")
"""


def _locus(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "locus", *args], capture_output=True,
        text=True,
        check=False,
    )


def _create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
    return ModelResponse(text="hello", finish_reason="end_turn", usage=Usage())


def _record(store: Path, name: str = "demo") -> str:
    with locus.record(name, store=store) as rec:
        model = rec.model(provider="acme", model_id="acme-1", create_fn=_create)
        model.create(messages=[Message(role="user", content="hi")])
        rec.outcome(status="ok")
        return rec.run_id


def test_ls_lists_runs_newest_first(tmp_path: Path) -> None:
    _record(tmp_path, "older")
    _record(tmp_path, "newer")
    result = _locus("ls", "--store", str(tmp_path))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().split("\n")
    assert lines[0].endswith("newer") and lines[1].endswith("older")
    assert "acme-1" in lines[0]


def test_ls_says_so_when_there_is_nothing(tmp_path: Path) -> None:
    assert "no runs in" in _locus("ls", "--store", str(tmp_path)).stdout


def test_export_and_import_move_a_run_between_stores(tmp_path: Path) -> None:
    source, target = tmp_path / "a", tmp_path / "b"
    run_id = _record(source)

    exported = _locus("export", run_id, "-o", str(tmp_path / "cassette"), "--store", str(source))
    assert exported.returncode == 0, exported.stderr
    assert "exported" in exported.stdout

    imported = _locus("import", str(tmp_path / "cassette"), "--store", str(target))
    assert imported.returncode == 0, imported.stderr
    assert run_id in imported.stdout

    db = Store(target)
    assert db.run(run_id).name == "demo"
    db.close()


def test_export_accepts_a_cassette_name(tmp_path: Path) -> None:
    _record(tmp_path / "a", "by-name")
    result = _locus(
        "export", "by-name", "-o", str(tmp_path / "cassette"), "--store", str(tmp_path / "a")
    )
    assert result.returncode == 0, result.stderr


def test_an_unknown_run_lists_what_exists(tmp_path: Path) -> None:
    _record(tmp_path, "demo")
    result = _locus("export", "nope", "-o", str(tmp_path / "c"), "--store", str(tmp_path))
    assert result.returncode != 0
    assert "demo" in result.stderr + result.stdout


def test_an_error_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    _record(tmp_path, "demo")
    result = _locus("export", "nope", "-o", str(tmp_path / "c"), "--store", str(tmp_path))
    assert result.stderr.startswith("locus: no run or cassette named 'nope'")
    assert "Traceback" not in result.stderr
    assert "KeyError" not in result.stderr


def test_the_id_that_ls_prints_is_accepted_back(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script))

    listed = _locus("ls", "--store", str(store)).stdout.split()[0]
    result = _locus("replay", listed, "--store", str(store))
    assert result.returncode == 0, result.stderr
    assert "from the script" in result.stdout


def test_an_ambiguous_run_id_prefix_says_so(tmp_path: Path) -> None:
    db = Store(tmp_path)
    for suffix in ("aa", "bb"):
        db.create_run(
            locus.RunHeader(
                run_id=f"beef{suffix}", name=f"run-{suffix}", started_at=1.0, status="ok"
            )
        )
    db.close()
    result = _locus("export", "beef", "-o", str(tmp_path / "c"), "--store", str(tmp_path))
    assert result.returncode != 0
    assert "matches more than one run" in result.stderr


def test_the_wrapper_records_a_script_that_opens_its_own_session(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"

    result = _locus(
        "record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script)
    )
    assert result.returncode == 0, result.stderr
    assert "from the script" in result.stdout

    db = Store(store)
    header = db.latest_named("wrapped")
    # The wrapper owns the run, so the script's own cassette name is not used.
    assert header.name == "wrapped"
    assert header.command == [sys.executable, str(script)]
    assert len(db.runs()) == 1
    db.close()


def test_replay_reruns_the_recorded_command_without_being_told_it(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script))
    run_id = Store(store).latest_named("wrapped").run_id

    result = _locus("replay", run_id, "--store", str(store))
    assert result.returncode == 0, result.stderr
    assert "from the script" in result.stdout


def test_replay_says_how_many_calls_it_answered_from_the_log(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script))
    run_id = Store(store).latest_named("wrapped").run_id

    result = _locus("replay", run_id, "--store", str(store))
    assert result.returncode == 0, result.stderr
    assert "1 matched, 0 degraded, 0 missed" in result.stdout


def test_recording_afresh_reports_no_replay_counts(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"
    result = _locus(
        "record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script)
    )
    assert result.returncode == 0, result.stderr
    assert "matched" not in result.stdout


def test_a_replay_the_agent_walked_away_from_reports_the_miss(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "wrapped", "--", sys.executable, str(script))
    run_id = Store(store).latest_named("wrapped").run_id

    # Same shape, different prompt: the request no longer hashes to a recorded
    # one. The child dies on the miss, so this also proves the counts survive a
    # child that exits non-zero.
    drifted = tmp_path / "drifted.py"
    drifted.write_text(SCRIPT.replace('content="hi"', 'content="different"'))
    result = _locus("replay", run_id, "--store", str(store), "--", sys.executable, str(drifted))
    assert result.returncode != 0
    assert "0 matched, 0 degraded, 1 missed, 1 recorded call unused" in result.stdout


def test_replay_needs_a_command_when_the_run_was_recorded_from_the_library(
    tmp_path: Path,
) -> None:
    run_id = _record(tmp_path)
    result = _locus("replay", run_id, "--store", str(tmp_path))
    assert result.returncode != 0
    assert "no command to re-run" in result.stderr + result.stdout


def test_a_failing_program_is_recorded_as_a_failed_run(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text(SCRIPT + "\nraise SystemExit(3)\n")
    store = tmp_path / "store"

    result = _locus(
        "record", "--store", str(store), "--name", "boom", "--", sys.executable, str(script)
    )
    assert result.returncode == 3
    db = Store(store)
    assert db.latest_named("boom").status == "error"
    db.close()


def test_redaction_is_on_unless_the_escape_hatch_is_used(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)

    default_store, open_store = tmp_path / "default", tmp_path / "open"
    _locus("record", "--store", str(default_store), "--", sys.executable, str(script))
    _locus(
        "record", "--no-redact", "--store", str(open_store), "--", sys.executable, str(script)
    )

    assert Store(default_store).runs()[0].redacted is True
    assert Store(open_store).runs()[0].redacted is False


def test_an_unknown_mode_is_rejected_before_anything_runs(tmp_path: Path) -> None:
    result = _locus(
        "record", "--store", str(tmp_path), "--mode", "sometimes", "--", sys.executable, "-c", ""
    )
    assert result.returncode != 0
    assert "new_episodes" in result.stderr + result.stdout
