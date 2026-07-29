from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(v) for v in value]
    return value


# Canonical serialization sorts keys, so a mapping that round-trips through the
# store comes back in a different *insertion* order than the one recorded. Agent
# code that serializes tool arguments would then see different bytes on replay.
# Normalizing at the schema boundary makes the record and replay paths agree.
CanonicalValue = Annotated[JsonValue, BeforeValidator(_sort_keys)]
CanonicalMapping = Annotated[dict[str, JsonValue], BeforeValidator(_sort_keys)]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobRef(BaseModel):
    digest: str
    size: int


class EventMeta(BaseModel):
    """Observational data about recording. Never part of an event's canonical bytes.

    Wall clocks and durations differ on every run by definition, so keeping them
    out of the canonical payload is what makes byte-identity checkable at all.
    Chunk offsets live here too: inter-chunk timing is recorded, but deliberately
    never reproduced on replay.
    """

    recorded_at: float
    duration_ms: float | None = None
    chunk_offsets_ms: list[float] | None = None


class Event(BaseModel):
    type: str
    parent_call_id: str | None = None
    meta: EventMeta

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json", exclude={"meta"})).encode("utf-8")


Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    role: Role
    content: str
    tool_call_id: str | None = None
    # Free at record time and impossible to reconstruct afterward, so it is
    # captured before anything reads it. Per-block token attribution needs it.
    provenance: str | None = None


class DecodeParams(BaseModel):
    # A misspelled decode param must not be silently dropped: it would change
    # the request without changing messages_hash, so replay would match a call
    # that was never made.
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    seed: int | None = None


class ToolCallRequest(BaseModel):
    id: str
    name: str
    args: CanonicalMapping
    # Parallel batches give a partial order, not a total one. Position within
    # the batch is the stable identity; arrival order is not.
    batch_index: int


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    finish_reason: str
    usage: Usage = Field(default_factory=Usage)
    logprobs: list[CanonicalValue] | None = None


class StreamChunk(BaseModel):
    index: int
    text_delta: str = ""
    tool_call_delta: CanonicalMapping | None = None


class StreamRecord(BaseModel):
    chunks: list[StreamChunk]


class ToolOutcome(BaseModel):
    content: str
    status: Literal["ok", "error"] = "ok"
    error: str | None = None


def hash_messages(messages: list[Message]) -> str:
    # Provenance is an annotation about where a block came from, not something
    # the model saw, so it must not affect whether two requests match.
    payload = [m.model_dump(mode="json", exclude={"provenance"}) for m in messages]
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def hash_args(args: dict[str, JsonValue]) -> str:
    return sha256_hex(canonical_json(args).encode("utf-8"))


class ModelCallEvent(Event):
    type: Literal["model_call"] = "model_call"
    call_id: str
    provider: str
    model_id: str
    params: DecodeParams
    messages: list[Message]
    messages_hash: str
    response: ModelResponse
    stream: StreamRecord | None = None

    @model_validator(mode="after")
    def _stream_reassembles(self) -> ModelCallEvent:
        if self.stream is None:
            return self
        assembled = "".join(c.text_delta for c in self.stream.chunks)
        if assembled != self.response.text:
            raise ValueError(
                f"stream chunks for call {self.call_id} do not reassemble to the recorded "
                f"response: {len(assembled)} chars from {len(self.stream.chunks)} chunks vs "
                f"{len(self.response.text)} chars in response.text. The stream adapter is "
                f"dropping or duplicating deltas; fix it before recording."
            )
        return self


class ToolCallEvent(Event):
    type: Literal["tool_call"] = "tool_call"
    parent_call_id: str
    tool_call_id: str
    batch_index: int
    name: str
    args: CanonicalMapping
    args_hash: str
    result: BlobRef
    status: Literal["ok", "error"]
    error: str | None = None


class EnvironmentEvent(Event):
    type: Literal["environment"] = "environment"
    source: Literal["clock", "monotonic", "random", "uuid", "env"]
    key: str | None = None
    value: CanonicalValue


class OutcomeEvent(Event):
    type: Literal["outcome"] = "outcome"
    status: Literal["ok", "error"]
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)


AnyEvent = Annotated[
    ModelCallEvent | ToolCallEvent | EnvironmentEvent | OutcomeEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


class RunHeader(BaseModel):
    run_id: str
    name: str
    started_at: float
    finished_at: float | None = None
    status: Literal["running", "ok", "error"]
    schema_version: int = SCHEMA_VERSION


class StoredEvent(BaseModel):
    run_id: str
    seq: int
    event: AnyEvent


def canonical_order(events: list[StoredEvent]) -> list[StoredEvent]:
    """Order a run's events deterministically.

    Not `seq` order. Sequence numbers are assigned at insert time, so a parallel
    tool batch numbers its calls by completion, which is
    nondeterministic. Group children under their parent model call and order
    them by `batch_index` instead. Events that are neither a model call nor a
    child of one still fall back to `seq`, which is only deterministic if
    nothing concurrent produced them.
    """
    seq_of_call = {
        e.event.call_id: e.seq for e in events if isinstance(e.event, ModelCallEvent)
    }

    def key(e: StoredEvent) -> tuple[int, int, int, int]:
        ev = e.event
        if isinstance(ev, ModelCallEvent):
            return (e.seq, 0, 0, 0)
        group = seq_of_call.get(ev.parent_call_id, e.seq) if ev.parent_call_id else e.seq
        batch = ev.batch_index if isinstance(ev, ToolCallEvent) else 0
        return (group, 1, batch, e.seq)

    return sorted(events, key=key)


def run_digest(events: list[StoredEvent]) -> str:
    h = hashlib.sha256()
    for e in canonical_order(events):
        h.update(e.event.canonical_bytes())
        h.update(b"\n")
    return h.hexdigest()
