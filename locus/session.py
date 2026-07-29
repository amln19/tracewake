from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self
from uuid import uuid4

from .events import (
    AnyEvent,
    DecodeParams,
    EnvironmentEvent,
    EventMeta,
    Message,
    ModelCallEvent,
    ModelResponse,
    OutcomeEvent,
    StoredEvent,
    StreamChunk,
    StreamRecord,
    ToolCallEvent,
    ToolCallRequest,
    ToolOutcome,
    Usage,
    hash_args,
    hash_messages,
)
from .store import Store

CreateFn = Callable[[str, list[Message], DecodeParams], ModelResponse]
StreamFn = Callable[[str, list[Message], DecodeParams], Generator[StreamChunk, None, ModelResponse]]
DispatchFn = Callable[[str, dict[str, Any]], ToolOutcome]


class ReplayMiss(Exception):
    """The replayed agent asked for something the recorded run does not contain."""


@dataclass(frozen=True)
class Completion:
    call_id: str
    response: ModelResponse


class _GeneratorSource:
    """Drives a stream generator and captures its return value.

    Provider SDKs hand back the assembled message separately from the chunk
    iterator. Expressing that as a generator return keeps the backend contract
    to one object, so record and replay can share `StreamHandle` unchanged.
    """

    def __init__(self, gen: Generator[StreamChunk, None, ModelResponse]) -> None:
        self._gen = gen
        self.response: ModelResponse | None = None

    def __iter__(self) -> Generator[StreamChunk]:
        self.response = yield from self._gen


class StreamHandle:
    """The streaming interface the agent sees. Identical on record and replay.

    Replay re-emits recorded chunks through this same object with no delay.
    Original inter-chunk timing is recorded into `EventMeta` and never
    reproduced — it is not semantically meaningful and would make replay slow.
    """

    def __init__(
        self,
        call_id: str,
        source: Iterator[StreamChunk],
        finalize: Callable[[list[StreamChunk], list[float]], ModelResponse],
    ) -> None:
        self.call_id = call_id
        self._source = source
        self._finalize = finalize
        self._chunks: list[StreamChunk] = []
        self._offsets: list[float] = []
        self._response: ModelResponse | None = None
        self._start = time.perf_counter()

    def __iter__(self) -> StreamHandle:
        return self

    def __next__(self) -> StreamChunk:
        if self._response is not None:
            raise StopIteration
        try:
            chunk = next(self._source)
        except StopIteration:
            self._response = self._finalize(self._chunks, self._offsets)
            raise
        self._chunks.append(chunk)
        self._offsets.append((time.perf_counter() - self._start) * 1000.0)
        return chunk

    def drain(self) -> ModelResponse:
        for _ in self:
            pass
        return self.response

    @property
    def response(self) -> ModelResponse:
        if self._response is None:
            raise RuntimeError(
                "the stream has not been consumed to completion, so there is no assembled "
                "response yet. Iterate it fully or call drain() before reading .response."
            )
        return self._response

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # A caller that breaks out of the loop early still made the call, and an
        # unrecorded model call would silently break replay. Drain to record it.
        if exc_type is None:
            self.drain()
        return False


class RecordingModel:
    def __init__(
        self,
        session: Recorder,
        provider: str,
        model_id: str,
        create_fn: CreateFn,
        stream_fn: StreamFn,
    ) -> None:
        self._session = session
        self._provider = provider
        self._model_id = model_id
        self._create_fn = create_fn
        self._stream_fn = stream_fn

    def create(self, *, messages: list[Message], **params: Any) -> Completion:
        decode = DecodeParams(**params)
        messages = list(messages)
        started = time.perf_counter()
        response = self._create_fn(self._model_id, messages, decode)
        duration = (time.perf_counter() - started) * 1000.0
        call_id = self._session._append_model_call(
            provider=self._provider,
            model_id=self._model_id,
            params=decode,
            messages=messages,
            response=response,
            stream=None,
            duration_ms=duration,
            chunk_offsets_ms=None,
        )
        return Completion(call_id=call_id, response=response)

    def stream(self, *, messages: list[Message], **params: Any) -> StreamHandle:
        decode = DecodeParams(**params)
        # Snapshot the request: the event is written when the stream drains, and
        # agents append to their message list as they go. Hashing the mutated
        # list would record a request that was never sent, and replay hashes at
        # call time, so the two paths would disagree.
        messages = list(messages)
        call_id = uuid4().hex
        started = time.perf_counter()
        source = _GeneratorSource(self._stream_fn(self._model_id, messages, decode))

        def finalize(chunks: list[StreamChunk], offsets: list[float]) -> ModelResponse:
            if source.response is None:
                raise RuntimeError(
                    f"the stream backend for {self._model_id} finished without returning an "
                    f"assembled ModelResponse. A locus stream function must `return` the "
                    f"final response after yielding its chunks."
                )
            self._session._append_model_call(
                provider=self._provider,
                model_id=self._model_id,
                params=decode,
                messages=messages,
                response=source.response,
                stream=StreamRecord(chunks=chunks),
                duration_ms=(time.perf_counter() - started) * 1000.0,
                chunk_offsets_ms=offsets,
                call_id=call_id,
            )
            return source.response

        return StreamHandle(call_id, iter(source), finalize)


class RecordingTools:
    def __init__(self, session: Recorder, dispatch_fn: DispatchFn) -> None:
        self._session = session
        self._dispatch = dispatch_fn

    def call(self, parent_call_id: str, request: ToolCallRequest) -> ToolOutcome:
        started = time.perf_counter()
        outcome = self._dispatch(request.name, request.args)
        duration = (time.perf_counter() - started) * 1000.0
        ref = self._session._store.blobs.put(outcome.content.encode("utf-8"))
        self._session._append(
            ToolCallEvent(
                parent_call_id=parent_call_id,
                tool_call_id=request.id,
                batch_index=request.batch_index,
                name=request.name,
                args=request.args,
                args_hash=hash_args(request.args),
                result=ref,
                status=outcome.status,
                error=outcome.error,
                meta=EventMeta(recorded_at=time.time(), duration_ms=duration),
            )
        )
        return outcome


class RecordingClock:
    def __init__(self, session: Recorder) -> None:
        self._session = session

    def time(self) -> float:
        value = time.time()
        self._session._append_env("clock", value)
        return value

    def monotonic(self) -> float:
        value = time.monotonic()
        self._session._append_env("monotonic", value)
        return value


class Recorder:
    def __init__(self, store: Store, run_id: str, name: str) -> None:
        self.run_id = run_id
        self.name = name
        self._store = store
        # Events name the model call that caused them, so the log reconstructs
        # as a tree. Clock reads are attributed to the most recent call; tool
        # calls carry their parent explicitly.
        self._current_call_id: str | None = None
        self.clock = RecordingClock(self)

    def model(
        self,
        *,
        provider: str,
        model_id: str,
        create_fn: CreateFn,
        stream_fn: StreamFn,
    ) -> RecordingModel:
        return RecordingModel(self, provider, model_id, create_fn, stream_fn)

    def tools(self, dispatch_fn: DispatchFn) -> RecordingTools:
        return RecordingTools(self, dispatch_fn)

    def outcome(
        self,
        *,
        status: Literal["ok", "error"],
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        self._append(
            OutcomeEvent(
                status=status,
                error=error,
                usage=usage or Usage(),
                parent_call_id=self._current_call_id,
                meta=EventMeta(recorded_at=time.time()),
            )
        )

    def _append(self, event: AnyEvent) -> int:
        return self._store.append(self.run_id, event)

    def _append_env(self, source: str, value: Any, key: str | None = None) -> None:
        self._append(
            EnvironmentEvent(
                source=source,
                key=key,
                value=value,
                parent_call_id=self._current_call_id,
                meta=EventMeta(recorded_at=time.time()),
            )
        )

    def _append_model_call(
        self,
        *,
        provider: str,
        model_id: str,
        params: DecodeParams,
        messages: list[Message],
        response: ModelResponse,
        stream: StreamRecord | None,
        duration_ms: float,
        chunk_offsets_ms: list[float] | None,
        call_id: str | None = None,
    ) -> str:
        call_id = call_id or uuid4().hex
        self._append(
            ModelCallEvent(
                call_id=call_id,
                provider=provider,
                model_id=model_id,
                params=params,
                messages=messages,
                messages_hash=hash_messages(messages),
                response=response,
                stream=stream,
                parent_call_id=self._current_call_id,
                meta=EventMeta(
                    recorded_at=time.time(),
                    duration_ms=duration_ms,
                    chunk_offsets_ms=chunk_offsets_ms,
                ),
            )
        )
        self._current_call_id = call_id
        return call_id


class ReplayingModel:
    def __init__(self, session: Player, provider: str, model_id: str) -> None:
        self._session = session
        self._provider = provider
        self._model_id = model_id

    def create(self, *, messages: list[Message], **params: Any) -> Completion:
        DecodeParams(**params)
        recorded = self._session._match_model_call(self._model_id, messages)
        if recorded.stream is not None:
            raise ReplayMiss(
                f"call {recorded.call_id} was recorded as a stream but the replayed agent "
                f"called create(). Use stream() so the agent consumes it the same way it "
                f"did when recorded."
            )
        return Completion(call_id=recorded.call_id, response=recorded.response)

    def stream(self, *, messages: list[Message], **params: Any) -> StreamHandle:
        DecodeParams(**params)
        recorded = self._session._match_model_call(self._model_id, messages)
        if recorded.stream is None:
            raise ReplayMiss(
                f"call {recorded.call_id} was recorded without streaming, so there are no "
                f"chunk boundaries to re-emit. Synthesizing one would fabricate data the "
                f"run never produced. Use create(), or re-record with streaming."
            )
        return StreamHandle(
            recorded.call_id,
            iter(recorded.stream.chunks),
            lambda chunks, offsets: recorded.response,
        )


class ReplayingTools:
    def __init__(self, session: Player) -> None:
        self._session = session

    def call(self, parent_call_id: str, request: ToolCallRequest) -> ToolOutcome:
        # Keyed by (parent, tool id) rather than sequence, so a parallel batch
        # replays correctly no matter what order its calls complete in.
        recorded = self._session._tool_calls.get((parent_call_id, request.id))
        if recorded is None:
            raise ReplayMiss(
                f"no recorded result for tool {request.name!r} (id {request.id}) under model "
                f"call {parent_call_id} in run {self._session.run_id}."
            )
        args_hash = hash_args(request.args)
        if args_hash != recorded.args_hash:
            raise ReplayMiss(
                f"tool {request.name!r} (id {request.id}) was called with different arguments "
                f"than recorded: {args_hash[:12]} now vs {recorded.args_hash[:12]} in run "
                f"{self._session.run_id}. The replayed agent diverged."
            )
        content = self._session._store.blobs.get(recorded.result.digest).decode("utf-8")
        return ToolOutcome(content=content, status=recorded.status, error=recorded.error)


class ReplayingClock:
    def __init__(self, session: Player) -> None:
        self._session = session

    def time(self) -> float:
        return float(self._session._pop_env("clock"))

    def monotonic(self) -> float:
        return float(self._session._pop_env("monotonic"))


class Player:
    def __init__(self, store: Store, run_id: str) -> None:
        self.run_id = run_id
        self._store = store
        self._model_calls: dict[tuple[str, str], deque[ModelCallEvent]] = defaultdict(deque)
        self._tool_calls: dict[tuple[str, str], ToolCallEvent] = {}
        self._env: dict[str, deque[Any]] = defaultdict(deque)
        self._outcome: OutcomeEvent | None = None
        self._index(store.events(run_id))
        self.clock = ReplayingClock(self)

    def _index(self, events: list[StoredEvent]) -> None:
        for stored in events:
            ev = stored.event
            match ev:
                case ModelCallEvent():
                    self._model_calls[(ev.model_id, ev.messages_hash)].append(ev)
                case ToolCallEvent():
                    self._tool_calls[(ev.parent_call_id, ev.tool_call_id)] = ev
                case EnvironmentEvent():
                    self._env[ev.source].append(ev.value)
                case OutcomeEvent():
                    self._outcome = ev

    def model(
        self,
        *,
        provider: str,
        model_id: str,
        create_fn: CreateFn | None = None,
        stream_fn: StreamFn | None = None,
    ) -> ReplayingModel:
        # The backend functions are accepted and ignored so harness code is
        # byte-identical between record and replay. Replay must never call them.
        return ReplayingModel(self, provider, model_id)

    def tools(self, dispatch_fn: DispatchFn | None = None) -> ReplayingTools:
        return ReplayingTools(self)

    def outcome(
        self,
        *,
        status: Literal["ok", "error"],
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        if self._outcome is None:
            raise ReplayMiss(f"run {self.run_id} has no recorded outcome to replay against.")
        if status != self._outcome.status:
            raise ReplayMiss(
                f"replayed run ended {status!r} but was recorded as "
                f"{self._outcome.status!r}. The replayed agent diverged."
            )

    def _match_model_call(self, model_id: str, messages: list[Message]) -> ModelCallEvent:
        # Strict on (model, messages_hash); a miss is an error. Looser matching
        # is opt-in and never a silent fallback, because a fallback would hide
        # exactly the divergence this exists to surface.
        digest = hash_messages(messages)
        queue = self._model_calls.get((model_id, digest))
        if not queue:
            raise ReplayMiss(
                f"no unconsumed model call in run {self.run_id} matching model={model_id!r} "
                f"messages_hash={digest[:12]}. The replayed agent built a request the "
                f"recorded run never made."
            )
        return queue.popleft()

    def _pop_env(self, source: str) -> Any:
        queue = self._env.get(source)
        if not queue:
            raise ReplayMiss(
                f"run {self.run_id} has no unconsumed {source!r} value left. The replayed "
                f"agent read the {source} more times than the recorded run did."
            )
        return queue.popleft()
