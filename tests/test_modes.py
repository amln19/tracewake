from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import locus
from locus import DecodeParams, Message, ModelResponse, Store, Usage


class Backend:
    def __init__(self) -> None:
        self.calls = 0

    def create(
        self, model_id: str, messages: list[Message], params: DecodeParams
    ) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text=f"answer to {messages[-1].content}", finish_reason="end_turn", usage=Usage()
        )


def _ask(session: Any, backend: Backend, prompt: str) -> str:
    model = session.model(provider="p", model_id="m", create_fn=backend.create)
    return model.create(messages=[Message(role="user", content=prompt)]).response.text


def test_once_records_when_there_is_no_cassette_and_replays_when_there_is(
    tmp_path: Path,
) -> None:
    backend = Backend()
    with locus.session("greet", store=tmp_path, mode="once") as first:
        assert _ask(first, backend, "hi") == "answer to hi"
        first.outcome(status="ok")
        run_id = first.run_id
    assert backend.calls == 1

    with locus.session("greet", store=tmp_path, mode="once") as second:
        assert second.run_id == run_id, "the second open must reuse the cassette"
        assert _ask(second, backend, "hi") == "answer to hi"
        second.outcome(status="ok")
    assert backend.calls == 1, "the backend was called again despite a cassette"


def test_once_errors_on_a_request_the_cassette_does_not_contain(tmp_path: Path) -> None:
    backend = Backend()
    with locus.session("greet", store=tmp_path, mode="once") as first:
        _ask(first, backend, "hi")
        first.outcome(status="ok")

    with pytest.raises(locus.ReplayMiss, match="never made"):
        with locus.session("greet", store=tmp_path, mode="once") as second:
            _ask(second, backend, "something new")


def test_none_refuses_to_open_without_a_cassette(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="no run or cassette"):
        with locus.session("absent", store=tmp_path, mode="none"):
            pass


def test_none_never_records_even_when_a_backend_is_available(tmp_path: Path) -> None:
    backend = Backend()
    with locus.record("greet", store=tmp_path) as rec:
        _ask(rec, backend, "hi")
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(tmp_path)
    before = len(db.events(run_id))
    with pytest.raises(locus.ReplayMiss):
        with locus.session("greet", store=tmp_path, mode="none") as rep:
            _ask(rep, backend, "unrecorded")
    assert len(db.events(run_id)) == before
    assert backend.calls == 1
    db.close()


def test_new_episodes_replays_what_matches_and_records_what_does_not(
    tmp_path: Path,
) -> None:
    backend = Backend()
    with locus.record("greet", store=tmp_path) as rec:
        _ask(rec, backend, "hi")
        rec.outcome(status="ok")
        run_id = rec.run_id

    with locus.session("greet", store=tmp_path, mode="new_episodes") as rep:
        assert _ask(rep, backend, "hi") == "answer to hi"
        assert _ask(rep, backend, "and then?") == "answer to and then?"
        assert rep.report.matched == 1
        assert rep.report.recorded_new == 1
        assert rep.run_id == run_id, "the new episode belongs to the same cassette"
    assert backend.calls == 2

    # The branch the replay discovered is now part of the cassette.
    with locus.session("greet", store=tmp_path, mode="none") as strict:
        assert _ask(strict, backend, "and then?") == "answer to and then?"
    assert backend.calls == 2


def test_all_records_a_new_run_under_the_same_name(tmp_path: Path) -> None:
    backend = Backend()
    ids = []
    for _ in range(2):
        with locus.session("greet", store=tmp_path, mode="all") as rec:
            _ask(rec, backend, "hi")
            rec.outcome(status="ok")
            ids.append(rec.run_id)

    assert len(set(ids)) == 2
    assert backend.calls == 2
    db = Store(tmp_path)
    # Re-recording supersedes rather than deletes, so the older run stays
    # addressable while the name resolves to the newest.
    assert db.latest_named("greet").run_id == ids[1]
    assert len(db.runs()) == 2
    db.close()


def test_an_unknown_mode_names_the_ones_that_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="new_episodes"):
        with locus.session("x", store=tmp_path, mode="sometimes"):
            pass


def test_a_run_id_resolves_as_well_as_a_name(tmp_path: Path) -> None:
    backend = Backend()
    with locus.record("greet", store=tmp_path) as rec:
        _ask(rec, backend, "hi")
        rec.outcome(status="ok")
        run_id = rec.run_id

    with locus.session(run_id, store=tmp_path, mode="none") as rep:
        assert _ask(rep, backend, "hi") == "answer to hi"
