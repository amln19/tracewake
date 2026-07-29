"""The gate: a fully mocked agent run replays byte-identically."""

from __future__ import annotations

from pathlib import Path

import pytest
from mock_agent import (
    TASK,
    MockBackend,
    Transcript,
    forbidden_create,
    forbidden_dispatch,
    forbidden_stream,
    run_agent,
)

import locus
from locus import (
    DecodeParams,
    Message,
    ModelCallEvent,
    ModelResponse,
    Store,
    ToolCallEvent,
    ToolCallRequest,
    Usage,
    canonical_order,
    run_digest,
)


def record_run(store: Path, task: str = TASK) -> tuple[str, Transcript, MockBackend]:
    backend = MockBackend()
    transcript = Transcript()
    with locus.record("gate", store=store) as rec:
        model = rec.model(
            provider="mock",
            model_id="mock-1",
            create_fn=backend.create,
            stream_fn=backend.stream,
        )
        usage = run_agent(model, rec.tools(backend.dispatch), rec.clock, transcript, task)
        rec.outcome(status="ok", usage=usage)
        run_id = rec.run_id
    return run_id, transcript, backend


def replay_run(store: Path, run_id: str, task: str = TASK) -> Transcript:
    transcript = Transcript()
    with locus.replay(run_id, store=store) as rep:
        model = rep.model(
            provider="mock",
            model_id="mock-1",
            create_fn=forbidden_create,
            stream_fn=forbidden_stream,
        )
        usage = run_agent(model, rep.tools(forbidden_dispatch), rep.clock, transcript, task)
        rep.outcome(status="ok", usage=usage)
    return transcript


def test_mocked_run_replays_byte_identically(tmp_path: Path) -> None:
    run_id, recorded, backend = record_run(tmp_path)
    replayed = replay_run(tmp_path, run_id)

    assert replayed.to_bytes() == recorded.to_bytes()
    assert len(recorded.lines) > 40
    assert (backend.streams, backend.dispatches) == (3, 5)


def test_replay_is_stable_across_repeats(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    assert replay_run(tmp_path, run_id).to_bytes() == replay_run(tmp_path, run_id).to_bytes()


def test_clock_values_replay_exactly(tmp_path: Path) -> None:
    run_id, recorded, _ = record_run(tmp_path)
    replayed = replay_run(tmp_path, run_id)

    clock_lines = [ln for ln in recorded.lines if ln.startswith(("elapsed\t", "wall\t"))]
    assert len(clock_lines) == 4
    assert clock_lines == [ln for ln in replayed.lines if ln.startswith(("elapsed\t", "wall\t"))]
    # A real wall clock, not a frozen zero — replay reproduces the recorded value.
    assert float(clock_lines[1].split("\t")[1]) > 1_700_000_000


def test_stream_chunk_boundaries_replay_exactly(tmp_path: Path) -> None:
    run_id, recorded, _ = record_run(tmp_path)
    replayed = replay_run(tmp_path, run_id)

    chunks = [ln for ln in recorded.lines if ln.startswith("chunk\t")]
    assert len(chunks) > 20
    assert chunks == [ln for ln in replayed.lines if ln.startswith("chunk\t")]


def test_failed_tool_replays_with_status_and_error(tmp_path: Path) -> None:
    run_id, recorded, _ = record_run(tmp_path)
    replayed = replay_run(tmp_path, run_id)

    failures = [ln for ln in recorded.lines if "run_tests" in ln and '"error"' in ln]
    assert len(failures) == 1
    assert "pytest exited 1" in failures[0]
    assert failures == [ln for ln in replayed.lines if "run_tests" in ln and '"error"' in ln]


def test_parallel_batch_is_ordered_by_batch_index_not_arrival(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    store = Store(tmp_path)
    events = store.events(run_id)

    first_batch = [
        e for e in events if isinstance(e.event, ToolCallEvent) and e.event.batch_index is not None
    ][:3]
    arrival = [e.event.batch_index for e in first_batch]
    assert arrival != sorted(arrival), (
        "the mocked tools are meant to complete out of batch order; without that this "
        "test proves nothing"
    )

    ordered = [
        e.event.batch_index
        for e in canonical_order(events)
        if isinstance(e.event, ToolCallEvent)
    ]
    assert ordered[:3] == [0, 1, 2]
    assert ordered[3:] == [0, 1]
    store.close()


def test_canonical_bytes_survive_the_store_round_trip(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    store = Store(tmp_path)
    events = store.events(run_id)
    reread = Store(tmp_path).events(run_id)
    assert run_digest(events) == run_digest(reread)
    assert len({run_digest(events), run_digest(canonical_order(events))}) == 1
    store.close()


def test_divergent_request_raises_instead_of_replaying_something_else(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    with pytest.raises(locus.ReplayMiss, match="never made"):
        replay_run(tmp_path, run_id, task="a completely different task")


def test_divergent_tool_args_raise(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    store = Store(tmp_path)
    call = next(e.event for e in store.events(run_id) if isinstance(e.event, ModelCallEvent))
    recorded_tool = call.response.tool_calls[0]
    store.close()

    with locus.replay(run_id, store=tmp_path) as rep:
        tools = rep.tools()
        mutated = ToolCallRequest(
            id=recorded_tool.id,
            name=recorded_tool.name,
            args={"path": "src/somewhere_else.py"},
            batch_index=recorded_tool.batch_index,
        )
        with pytest.raises(locus.ReplayMiss, match="different arguments"):
            tools.call(call.call_id, mutated)


def test_unknown_tool_id_raises(tmp_path: Path) -> None:
    run_id, _, _ = record_run(tmp_path)
    with locus.replay(run_id, store=tmp_path) as rep:
        with pytest.raises(locus.ReplayMiss, match="no recorded result"):
            rep.tools().call(
                "nonexistent-call",
                ToolCallRequest(id="x", name="read_file", args={}, batch_index=0),
            )


def test_streaming_and_non_streaming_are_not_interchangeable(tmp_path: Path) -> None:
    messages = [Message(role="user", content="hi", provenance="user_task")]

    def create(model_id: str, msgs: list[Message], params: DecodeParams) -> ModelResponse:
        return ModelResponse(text="hello", finish_reason="end_turn", usage=Usage())

    with locus.record("nonstream", store=tmp_path) as rec:
        model = rec.model(
            provider="mock", model_id="mock-1", create_fn=create, stream_fn=forbidden_stream
        )
        assert model.create(messages=messages).response.text == "hello"
        rec.outcome(status="ok")
        run_id = rec.run_id

    with locus.replay(run_id, store=tmp_path) as rep:
        model = rep.model(provider="mock", model_id="mock-1")
        with pytest.raises(locus.ReplayMiss, match="no chunk boundaries"):
            model.stream(messages=messages)


def test_mutating_the_message_list_before_draining_does_not_corrupt_the_record(
    tmp_path: Path,
) -> None:
    def leaky(model: object, transcript: Transcript) -> None:
        messages = [Message(role="user", content="hi", provenance="user_task")]
        stream = model.stream(messages=messages)
        messages.append(Message(role="assistant", content="appended too early"))
        for chunk in stream:
            transcript.observe("chunk", chunk.model_dump_json())
        transcript.observe("response", stream.response.model_dump_json())

    backend = MockBackend()
    recorded = Transcript()
    with locus.record("leaky", store=tmp_path) as rec:
        leaky(
            rec.model(
                provider="mock",
                model_id="mock-1",
                create_fn=backend.create,
                stream_fn=backend.stream,
            ),
            recorded,
        )
        rec.outcome(status="ok")
        run_id = rec.run_id

    replayed = Transcript()
    with locus.replay(run_id, store=tmp_path) as rep:
        leaky(
            rep.model(
                provider="mock",
                model_id="mock-1",
                create_fn=forbidden_create,
                stream_fn=forbidden_stream,
            ),
            replayed,
        )
    assert replayed.to_bytes() == recorded.to_bytes()


def test_a_partially_consumed_stream_is_still_recorded(tmp_path: Path) -> None:
    def first_chunk_only(model: object, transcript: Transcript) -> None:
        messages = [Message(role="user", content="hi", provenance="user_task")]
        with model.stream(messages=messages) as stream:
            for chunk in stream:
                transcript.observe("chunk", chunk.model_dump_json())
                break

    backend = MockBackend()
    recorded = Transcript()
    with locus.record("partial", store=tmp_path) as rec:
        first_chunk_only(
            rec.model(
                provider="mock",
                model_id="mock-1",
                create_fn=backend.create,
                stream_fn=backend.stream,
            ),
            recorded,
        )
        rec.outcome(status="ok")
        run_id = rec.run_id

    store = Store(tmp_path)
    assert sum(isinstance(e.event, ModelCallEvent) for e in store.events(run_id)) == 1
    store.close()

    replayed = Transcript()
    with locus.replay(run_id, store=tmp_path) as rep:
        first_chunk_only(
            rep.model(
                provider="mock",
                model_id="mock-1",
                create_fn=forbidden_create,
                stream_fn=forbidden_stream,
            ),
            replayed,
        )
    assert replayed.to_bytes() == recorded.to_bytes()


def test_replay_of_a_missing_run_names_what_exists(tmp_path: Path) -> None:
    record_run(tmp_path)
    with pytest.raises(KeyError, match="Known runs"):
        with locus.replay("does-not-exist", store=tmp_path):
            pass
