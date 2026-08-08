"""Replaying a run with one context block removed, and continuing live."""

from __future__ import annotations

from pathlib import Path

import pytest

import tracewake
from tracewake import (
    InterventionEvent,
    ModelCallEvent,
    Store,
    export_cassette,
    import_cassette,
    run_digest,
)
from tracewake.session import ReplayMiss

from mock_agent import MockBackend, Transcript, run_agent


def _record(tmp_path: Path) -> tuple[str, MockBackend]:
    backend = MockBackend()
    with tracewake.record("agent", store=tmp_path, mode="all") as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=backend.create, stream_fn=backend.stream
        )
        run_agent(model, s.tools(backend.dispatch), s.clock, Transcript())
        s.outcome(status="ok")
        return s.run_id, backend


def _calls(store: Path, run_id: str) -> list[ModelCallEvent]:
    db = Store(store)
    events = [e.event for e in db.events(run_id)]
    db.close()
    return [e for e in events if isinstance(e, ModelCallEvent)]


def _digest(store: Path, run_id: str) -> tuple[int, str]:
    db = Store(store)
    events = db.events(run_id)
    db.close()
    return len(events), run_digest(events)


def test_turns_before_the_change_replay_and_the_rest_runs_live(tmp_path: Path):
    source, _ = _record(tmp_path)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        s.outcome(status="ok")
        forked = s.run_id
        dropped = s.blocks_dropped

    assert forked != source
    # Turn 0 matched the recorded call, so the model was never asked for it.
    # Turns 1 and 2 carry the edited context and cannot match, so they went live.
    assert live.streams == 2
    assert _calls(tmp_path, source) and len(_calls(tmp_path, forked)) == 2
    assert dropped > 0


def test_the_forked_run_holds_no_dropped_block_and_the_source_is_untouched(tmp_path: Path):
    source, _ = _record(tmp_path)
    before = _digest(tmp_path, source)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        forked = s.run_id

    assert _digest(tmp_path, source) == before
    for call in _calls(tmp_path, forked):
        assert not [m for m in call.messages if m.provenance == "tool_output"]
    # The source still has them, which is what makes the comparison a
    # counterfactual rather than two unrelated runs.
    assert [m for c in _calls(tmp_path, source) for m in c.messages if m.provenance == "tool_output"]


def test_the_fork_records_what_it_was_forked_from(tmp_path: Path):
    source, _ = _record(tmp_path)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        forked = s.run_id
        name = s.name

    db = Store(tmp_path)
    events = [e.event for e in db.events(forked)]
    header = db.run(forked)
    db.close()
    declared = [e for e in events if isinstance(e, InterventionEvent)]
    assert len(declared) == 1
    assert declared[0].source_run_id == source
    assert declared[0].drop_tags == ["tool_output"]
    assert declared[0].from_turn == 1
    assert name == "agent+drop-tool_output@1"
    assert header.name == name


def test_tools_re_execute_so_the_world_reaches_the_state_the_prefix_describes(tmp_path: Path):
    source, _ = _record(tmp_path)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        forked = s.run_id
        replayed_results = s.report.tool_calls_replayed

    # Nothing is served from the source log, including under the replayed turn.
    # A served result would skip the effect the call had on the working tree,
    # leaving later steps acting on a tree the replayed prefix never built.
    assert replayed_results == 0
    db = Store(tmp_path)
    calls = [e.event for e in db.events(forked) if e.event.type == "tool_call"]
    db.close()
    assert len(calls) == live.dispatches


def test_an_intervention_that_would_change_nothing_is_refused_before_it_runs(tmp_path: Path):
    source, _ = _record(tmp_path)

    with pytest.raises(ValueError) as caught:
        tracewake.plan_intervention(source, drop_tags=["repo_map"], store=tmp_path)
    message = str(caught.value)
    assert "repo_map" in message
    # The message names what is actually there, because guessing a tag is the
    # normal way to reach this.
    assert "tool_output" in message and "system_prompt" in message


def test_plan_describe_names_how_many_blocks_the_drop_will_remove(tmp_path: Path):
    source, _ = _record(tmp_path)
    plan = tracewake.plan_intervention(source, drop_tags=["tool_output"], from_turn=1, store=tmp_path)
    assert plan.blocks > 0
    assert f"{plan.blocks} block" in plan.describe()


def test_intervening_past_the_end_of_the_run_is_refused(tmp_path: Path):
    source, _ = _record(tmp_path)

    with pytest.raises(ValueError, match="no turn 99"):
        tracewake.plan_intervention(source, drop_tags=["tool_output"], from_turn=99, store=tmp_path)


def test_an_intervention_without_a_live_model_says_so(tmp_path: Path):
    source, _ = _record(tmp_path)
    live = MockBackend()

    with pytest.raises(ReplayMiss, match="stream_fn"):
        with tracewake.intervene(
            source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
        ) as s:
            model = s.model(provider="mock", model_id="mock-1")
            run_agent(model, s.tools(live.dispatch), s.clock, Transcript())


def test_an_intervention_without_live_tools_says_so(tmp_path: Path):
    source, _ = _record(tmp_path)
    live = MockBackend()

    # The replayed prefix still needs real tools, so this fails on the first
    # turn rather than at the point the context was changed.
    with pytest.raises(ReplayMiss, match="no dispatch function"):
        with tracewake.intervene(
            source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
        ) as s:
            model = s.model(provider="mock", model_id="mock-1", stream_fn=live.stream)
            run_agent(model, s.tools(), s.clock, Transcript())


def test_a_fork_can_write_to_a_different_store_than_it_reads(tmp_path: Path):
    """What lets a closed corpus be forked without being appended to."""
    source_store = tmp_path / "closed"
    fork_store = tmp_path / "forks"
    source, _ = _record(source_store)
    before = _digest(source_store, source)

    live = MockBackend()
    with tracewake.intervene(
        source,
        drop_tags=["tool_output"],
        from_turn=1,
        store=fork_store,
        source_store=source_store,
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        forked = s.run_id

    db = Store(source_store)
    closed = [h.run_id for h in db.runs()]
    db.close()
    assert closed == [source], "the source store grew"
    assert _digest(source_store, source) == before

    db = Store(fork_store)
    assert [h.run_id for h in db.runs()] == [forked]
    db.close()


def test_a_fork_survives_a_cassette_round_trip(tmp_path: Path):
    """The fork's own record of what it is has to travel with it."""
    source, _ = _record(tmp_path)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["tool_output"], from_turn=1, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        s.outcome(status="ok")
        forked = s.run_id

    db = Store(tmp_path)
    before = run_digest(db.events(forked))
    cassette = export_cassette(db, forked, tmp_path / "cassette")
    db.close()

    restored = Store(tmp_path / "restored")
    header = import_cassette(cassette, restored)
    events = restored.events(header.run_id)
    restored.close()

    assert run_digest(events) == before
    declared = [e.event for e in events if isinstance(e.event, InterventionEvent)]
    assert declared and declared[0].source_run_id == source


def test_dropping_from_turn_zero_makes_every_turn_live(tmp_path: Path):
    source, _ = _record(tmp_path)

    live = MockBackend()
    with tracewake.intervene(
        source, drop_tags=["system_prompt"], from_turn=0, store=tmp_path
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        forked = s.run_id

    assert live.streams == 3
    assert len(_calls(tmp_path, forked)) == 3
