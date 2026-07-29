from __future__ import annotations

import pytest
from pydantic import ValidationError

from locus import (
    DecodeParams,
    EventMeta,
    Message,
    ModelCallEvent,
    ModelResponse,
    StreamChunk,
    StreamRecord,
    ToolCallRequest,
    Usage,
    hash_messages,
)


def _meta() -> EventMeta:
    return EventMeta(recorded_at=1.0)


def _call(chunks: list[str], text: str) -> ModelCallEvent:
    return ModelCallEvent(
        call_id="c1",
        provider="mock",
        model_id="mock-1",
        params=DecodeParams(temperature=0.0),
        messages=[Message(role="user", content="hi")],
        messages_hash=hash_messages([Message(role="user", content="hi")]),
        response=ModelResponse(text=text, finish_reason="end_turn", usage=Usage()),
        stream=StreamRecord(
            chunks=[StreamChunk(index=i, text_delta=c) for i, c in enumerate(chunks)]
        ),
        meta=_meta(),
    )


def test_chunks_that_do_not_reassemble_are_rejected() -> None:
    with pytest.raises(ValidationError, match="do not reassemble"):
        _call(["hel", "lo"], "hello world")


def test_chunks_that_reassemble_are_accepted() -> None:
    assert _call(["hel", "lo"], "hello").stream is not None


def test_canonical_bytes_exclude_recording_metadata() -> None:
    a = _call(["hi"], "hi")
    b = a.model_copy(update={"meta": EventMeta(recorded_at=999.0, duration_ms=42.0)})
    assert a.canonical_bytes() == b.canonical_bytes()


def test_canonical_bytes_are_key_ordered_and_compact() -> None:
    raw = _call(["hi"], "hi").canonical_bytes()
    assert b", " not in raw and b'": ' not in raw
    assert raw.index(b'"call_id"') < raw.index(b'"messages"') < raw.index(b'"provider"')


def test_provenance_does_not_change_the_match_key() -> None:
    plain = [Message(role="user", content="hi")]
    tagged = [Message(role="user", content="hi", provenance="user_task")]
    assert hash_messages(plain) == hash_messages(tagged)


def test_message_content_does_change_the_match_key() -> None:
    assert hash_messages([Message(role="user", content="hi")]) != hash_messages(
        [Message(role="user", content="ho")]
    )


def test_misspelled_decode_param_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DecodeParams(temperture=0.0)


def test_mapping_key_order_is_normalized_at_the_schema_boundary() -> None:
    a = ToolCallRequest(
        id="t", name="grep", args={"pattern": "x", "path": {"b": 1, "a": 2}}, batch_index=0
    )
    b = ToolCallRequest(
        id="t", name="grep", args={"path": {"a": 2, "b": 1}, "pattern": "x"}, batch_index=0
    )
    assert list(a.args) == ["path", "pattern"]
    assert list(a.args["path"]) == ["a", "b"]
    # Agent code that serializes args must see the same bytes before and after a
    # round trip through the store, which sorts keys.
    assert a.model_dump_json() == b.model_dump_json()
