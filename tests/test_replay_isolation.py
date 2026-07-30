"""A replay may not read the live filesystem, and may not write to its recording."""

from __future__ import annotations

import pytest

import locus
from locus import Message, ModelResponse, Store, Usage, run_digest


def backend(model_id, messages, params):
    return ModelResponse(text="ok", finish_reason="end_turn", usage=Usage())


def ask(session):
    model = session.model(provider="p", model_id="m", create_fn=backend)
    return model.create(messages=[Message(role="user", content="go")])


@pytest.fixture
def recorded(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "recorded.txt").write_text("recorded", encoding="utf-8")
    (work / "unrecorded.txt").write_text("live secret", encoding="utf-8")
    store = tmp_path / "store"
    with locus.record("probe", store=store, block_network=False) as session:
        ask(session)
        session.fs.rooted(work).read_text("recorded.txt")
    return (store, work)


def test_reading_a_file_the_recording_never_read_raises(recorded):
    store, work = recorded
    with pytest.raises(locus.ReplayMiss, match="never accessed"):
        with locus.replay("probe", store=store) as session:
            ask(session)
            session.fs.rooted(work).read_text("unrecorded.txt")


def test_a_replay_never_returns_live_file_contents(recorded):
    store, work = recorded
    (work / "recorded.txt").write_text("changed on disk", encoding="utf-8")
    with locus.replay("probe", store=store) as session:
        ask(session)
        assert session.fs.rooted(work).read_text("recorded.txt") == "recorded"


def test_listing_a_directory_the_recording_never_listed_raises(recorded):
    store, work = recorded
    with pytest.raises(locus.ReplayMiss, match="never accessed"):
        with locus.replay("probe", store=store) as session:
            ask(session)
            session.fs.rooted(work).listdir(".")


def test_writing_a_file_the_recording_never_wrote_raises(recorded):
    store, work = recorded
    with pytest.raises(locus.ReplayMiss, match="never accessed"):
        with locus.replay("probe", store=store) as session:
            ask(session)
            session.fs.rooted(work).write_text("fresh.txt", "written during replay")
    assert not (work / "fresh.txt").exists()


def test_a_replayed_run_is_unchanged_by_the_replay(recorded):
    store, work = recorded
    db = Store(store)
    run_id = db.find("probe").run_id
    before = db.events(run_id)
    db.close()

    with pytest.raises(locus.ReplayMiss):
        with locus.replay("probe", store=store) as session:
            ask(session)
            session.fs.rooted(work).read_text("unrecorded.txt")

    db = Store(store)
    after = db.events(run_id)
    db.close()
    assert len(after) == len(before)
    assert run_digest(after) == run_digest(before)


def test_a_divergent_replay_can_still_be_captured_as_a_new_episode(recorded):
    store, work = recorded
    with locus.session("probe", store=store, mode="new_episodes", block_network=False) as session:
        ask(session)
        assert session.fs.rooted(work).read_text("unrecorded.txt") == "live secret"
