from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import locus
from locus import Message, ModelResponse, Store, ToolCallRequest, ToolOutcome


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "window.py").write_text("def slice_window(xs, i, n):\n    return xs[i:i+n]\n")
    (root / "src" / "shapes.py").write_text("SHAPES = []\n")
    return root


def _record(store: Path, repo: Path, body: Any) -> str:
    with locus.record("fs", store=store) as rec:
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

    with locus.replay(run_id, store=tmp_path / "store") as rep:
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

    with locus.replay(run_id, store=tmp_path / "store") as rep:
        rep.fs.write_text(target, "the patch\n")
    assert not target.exists(), "replay wrote to the filesystem"


def test_writing_something_else_on_replay_is_divergence(tmp_path: Path, repo: Path) -> None:
    target = repo / "out" / "patch.diff"
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.write_text(target, "the patch\n")
    )
    with pytest.raises(locus.ReplayMiss, match="different content"):
        with locus.replay(run_id, store=tmp_path / "store") as rep:
            rep.fs.write_text(target, "a different patch\n")


def test_listings_are_sorted_and_replayed(tmp_path: Path, repo: Path) -> None:
    run_id = _record(tmp_path / "store", repo, lambda s: s.fs.listdir(repo / "src"))
    (repo / "src" / "extra.py").write_text("")

    with locus.replay(run_id, store=tmp_path / "store") as rep:
        assert rep.fs.listdir(repo / "src") == ["shapes.py", "window.py"]


def test_existence_checks_replay(tmp_path: Path, repo: Path) -> None:
    def body(session: Any) -> None:
        assert session.fs.exists(repo / "src" / "window.py")
        assert not session.fs.exists(repo / "src" / "nope.py")

    run_id = _record(tmp_path / "store", repo, body)
    (repo / "src" / "nope.py").write_text("it exists now")

    with locus.replay(run_id, store=tmp_path / "store") as rep:
        assert rep.fs.exists(repo / "src" / "window.py")
        assert not rep.fs.exists(repo / "src" / "nope.py")


def test_a_missing_file_is_missing_on_replay_too(tmp_path: Path, repo: Path) -> None:
    absent = repo / "src" / "absent.py"

    def body(session: Any) -> None:
        with pytest.raises(FileNotFoundError):
            session.fs.read_text(absent)

    run_id = _record(tmp_path / "store", repo, body)
    absent.write_text("appeared later")

    with locus.replay(run_id, store=tmp_path / "store") as rep:
        with pytest.raises(FileNotFoundError):
            rep.fs.read_text(absent)


def test_reading_more_often_than_the_recorded_run_is_divergence(
    tmp_path: Path, repo: Path
) -> None:
    run_id = _record(
        tmp_path / "store", repo, lambda s: s.fs.read_text(repo / "src" / "window.py")
    )
    with pytest.raises(locus.ReplayMiss, match="more times than"):
        with locus.replay(run_id, store=tmp_path / "store") as rep:
            rep.fs.read_text(repo / "src" / "window.py")
            rep.fs.read_text(repo / "src" / "window.py")


def test_a_read_inside_a_tool_is_attributed_to_that_tool(tmp_path: Path, repo: Path) -> None:
    def dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(content=session.fs.read_text(repo / "src" / args["file"]))

    store = tmp_path / "store"
    with locus.record("fs", store=store) as rec:
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


def test_the_home_directory_is_not_in_a_recorded_path(tmp_path: Path) -> None:
    target = Path.home() / ".locus-fs-test-file"
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
