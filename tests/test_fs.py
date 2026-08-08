from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

import tracewake
from tracewake import Message, ModelResponse, Store, ToolCallRequest, ToolOutcome


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "window.py").write_text("def slice_window(xs, i, n):\n    return xs[i:i+n]\n")
    (root / "src" / "shapes.py").write_text("SHAPES = []\n")
    return root


def _record(store: Path, repo: Path, body: Any) -> str:
    with tracewake.record("fs", store=store) as rec:
        body(rec)
        rec.outcome(status="ok")
        return rec.run_id


def test_reads_replay_from_the_log(tmp_path: Path, repo: Path) -> None:
    source = repo / "src" / "window.py"

    def body(session: Any) -> None:
        session.fs.read_text(source)

    run_id = _record(tmp_path / "store", repo, body)
    original = source.read_text()
    source.write_text("completely different content now")

    with tracewake.replay(run_id, store=tmp_path / "store") as rep:
        assert rep.fs.read_text(source) == original


def test_a_read_records_path_and_content_hash(tmp_path: Path, repo: Path) -> None:
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.read_text(repo / "src" / "window.py")
    )
    db = Store(tmp_path / "store")
    (event,) = [e.event for e in db.events(run_id) if e.event.type == "fs_read"]
    db.close()
    assert event.kind == "content"
    assert event.exists
    assert event.content.digest and event.content.size > 0
    assert event.path.endswith("src/window.py")


def test_a_write_is_verified_on_replay_and_not_repeated(tmp_path: Path, repo: Path) -> None:
    target = repo / "out" / "patch.diff"

    def body(session: Any) -> None:
        session.fs.write_text(target, "the patch\n")

    run_id = _record(tmp_path / "store", repo, body)
    assert target.read_text() == "the patch\n"
    target.unlink()

    with tracewake.replay(run_id, store=tmp_path / "store") as rep:
        rep.fs.write_text(target, "the patch\n")
    assert not target.exists(), "replay wrote to the filesystem"


def test_writing_something_else_on_replay_is_divergence(tmp_path: Path, repo: Path) -> None:
    target = repo / "out" / "patch.diff"
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.write_text(target, "the patch\n")
    )
    with pytest.raises(tracewake.ReplayMiss, match="different content"):
        with tracewake.replay(run_id, store=tmp_path / "store") as rep:
            rep.fs.write_text(target, "a different patch\n")


def test_listings_are_sorted_and_replayed(tmp_path: Path, repo: Path) -> None:
    run_id = _record(tmp_path / "store", repo, lambda s: s.fs.listdir(repo / "src"))
    (repo / "src" / "extra.py").write_text("")

    with tracewake.replay(run_id, store=tmp_path / "store") as rep:
        assert rep.fs.listdir(repo / "src") == ["shapes.py", "window.py"]


def test_existence_checks_replay(tmp_path: Path, repo: Path) -> None:
    def body(session: Any) -> None:
        assert session.fs.exists(repo / "src" / "window.py")
        assert not session.fs.exists(repo / "src" / "nope.py")

    run_id = _record(tmp_path / "store", repo, body)
    (repo / "src" / "nope.py").write_text("it exists now")

    with tracewake.replay(run_id, store=tmp_path / "store") as rep:
        assert rep.fs.exists(repo / "src" / "window.py")
        assert not rep.fs.exists(repo / "src" / "nope.py")


def test_a_missing_file_is_missing_on_replay_too(tmp_path: Path, repo: Path) -> None:
    absent = repo / "src" / "absent.py"

    def body(session: Any) -> None:
        with pytest.raises(FileNotFoundError):
            session.fs.read_text(absent)

    run_id = _record(tmp_path / "store", repo, body)
    absent.write_text("appeared later")

    with tracewake.replay(run_id, store=tmp_path / "store") as rep:
        with pytest.raises(FileNotFoundError):
            rep.fs.read_text(absent)


def test_reading_more_often_than_the_recorded_run_is_divergence(
    tmp_path: Path, repo: Path
) -> None:
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.read_text(repo / "src" / "window.py")
    )
    with pytest.raises(tracewake.ReplayMiss, match="more times than"):
        with tracewake.replay(run_id, store=tmp_path / "store") as rep:
            rep.fs.read_text(repo / "src" / "window.py")
            rep.fs.read_text(repo / "src" / "window.py")


def test_a_read_inside_a_tool_is_attributed_to_that_tool(tmp_path: Path, repo: Path) -> None:
    def dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(content=session.fs.read_text(repo / "src" / args["file"]))

    store = tmp_path / "store"
    with tracewake.record("fs", store=store) as rec:
        session = rec
        model = rec.model(
            provider="p",
            model_id="m",
            create_fn=lambda *a: ModelResponse(text="", finish_reason="tool_use"),
        )
        completion = model.create(messages=[Message(role="user", content="go")])
        rec.tools(dispatch).call(
            completion.call_id,
            ToolCallRequest(id="t0", name="read", args={"file": "window.py"}, batch_index=0),
        )
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(store)
    (read,) = [e.event for e in db.events(run_id) if e.event.type == "fs_read"]
    db.close()
    assert read.tool_call_id == "t0"
    assert read.parent_call_id == completion.call_id


def test_a_root_records_paths_relative_to_it(tmp_path: Path, repo: Path) -> None:
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.rooted(repo).read_text("src/window.py")
    )
    db = Store(tmp_path / "store")
    (event,) = [e.event for e in db.events(run_id) if e.event.type == "fs_read"]
    db.close()
    assert event.path == "src/window.py"


def test_two_working_copies_of_one_repo_record_the_same_paths(tmp_path: Path, repo: Path) -> None:
    def paths_for(copy: Path) -> list[str]:
        shutil.copytree(repo, copy)
        run_id = _record(
            tmp_path / "store", copy, lambda s: s.fs.rooted(copy).read_text("src/window.py")
        )
        db = Store(tmp_path / "store")
        found = [e.event.path for e in db.events(run_id) if e.event.type == "fs_read"]
        db.close()
        return found

    assert paths_for(tmp_path / "run-a") == paths_for(tmp_path / "run-b") == ["src/window.py"]


def test_reading_outside_the_root_is_refused(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "secrets.txt"
    outside.write_text("not the agent's business")

    with tracewake.record("fs", store=tmp_path / "store") as rec:
        rooted = rec.fs.rooted(repo)
        with pytest.raises(ValueError, match="outside the root"):
            rooted.read_text("../secrets.txt")
        with pytest.raises(ValueError, match="outside the root"):
            rooted.read_text(outside)
        rec.outcome(status="ok")


def test_the_home_directory_is_not_in_a_recorded_path(tmp_path: Path) -> None:
    target = Path.home() / ".tracewake-fs-test-file"
    target.write_text("x")
    try:
        run_id = _record(tmp_path / "store", tmp_path, lambda s: s.fs.read_text(target))
    finally:
        target.unlink()

    db = Store(tmp_path / "store")
    (event,) = [e.event for e in db.events(run_id) if e.event.type == "fs_read"]
    db.close()
    assert str(Path.home()) not in event.path
    assert event.path.startswith("<HOME>")
