from __future__ import annotations

import warnings
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, NoReturn, Self

from .config import Config, RecordMode
from .events import (
    AnyEvent,
    BlobRef,
    DecodeParams,
    EnvironmentEvent,
    EventMeta,
    FsReadEvent,
    FsWriteEvent,
    Message,
    ModelCallEvent,
    ModelIdentity,
    ModelResponse,
    OutcomeEvent,
    RunHeader,
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
from .fs import Fs, tool_scope
from .matching import CallMatcher, ReplayReport, build_request
from .patches import (
    TracewakeError,
    real_monotonic,
    real_perf_counter,
    real_time,
    real_uuid4,
)
from .redaction import Redactor
from .store import Store

CreateFn = Callable[[str, list[Message], DecodeParams], ModelResponse]
StreamFn = Callable[[str, list[Message], DecodeParams], Generator[StreamChunk, None, ModelResponse]]
DispatchFn = Callable[[str, dict[str, Any]], ToolOutcome]

SECONDS_PER_DAY = 86400.0


class ReplayMiss(TracewakeError):
    """The replayed agent asked for something the recorded run does not contain."""


class CassetteStale(UserWarning):
    """A cassette old enough that the model behind it may have changed."""


@dataclass(frozen=True)
class Completion:
    call_id: str
    response: ModelResponse


@dataclass(frozen=True)
class Intervention:
    """One change to what the agent sees, applied from a chosen turn onward.

    Dropping a context block changes the request, so the recorded call for that
    turn stops matching and the run continues against the live model. Turns
    before `from_turn` are untouched and still replay from the log, which is
    what makes the counterfactual cost only the inference after the change.
    """

    source_run_id: str
    drop_tags: frozenset[str]
    from_turn: int = 0
    # How many tagged messages the drop will remove — counted at plan time so
    # the CLI can say the change will bite before any inference is spent.
    blocks: int = 0

    def label(self) -> str:
        tags = "+".join(sorted(self.drop_tags))
        return f"drop-{tags}@{self.from_turn}"

    def describe(self) -> str:
        tags = ", ".join(sorted(self.drop_tags))
        unit = "block" if self.blocks == 1 else "blocks"
        suffix = f" ({self.blocks} {unit})" if self.blocks else ""
        return (
            f"dropping {tags} from turn {self.from_turn} of run "
            f"{self.source_run_id[:12]}{suffix}"
        )


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
        self._start = real_perf_counter()

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
        self._offsets.append((real_perf_counter() - self._start) * 1000.0)
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
        # A caller that breaks out early, or a backend that fails mid-stream,
        # still made a model call. An unrecorded call silently breaks replay.
        if self._response is not None:
            return False
        if exc_type is None:
            self.drain()
        elif self._chunks:
            self._response = self._finalize(self._chunks, self._offsets)
        return False


class Model:
    def __init__(
        self,
        session: Session,
        provider: str,
        model_id: str,
        model_version: str | None,
        create_fn: CreateFn | None,
        stream_fn: StreamFn | None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._model_id = model_id
        self._model_version = model_version
        self._create_fn = create_fn
        self._stream_fn = stream_fn
        session._saw_model(
            ModelIdentity(provider=provider, model_id=model_id, model_version=model_version)
        )

    def _matched(self, messages: list[Message], tools: list[str] | None) -> ModelCallEvent | None:
        if self._session._matcher is None:
            return None
        # Matching happens on the redacted form: redaction rewrites content, so a
        # hash taken before it and one taken after it would never agree.
        request = build_request(
            self._model_id, self._session._redactor.messages(messages), tools
        )
        hit = self._session._matcher.match(request)
        if hit is None and not self._session.can_record:
            raise ReplayMiss(self._session._matcher.describe_miss(request, self._session.run_id))
        return hit

    def _backend(self, fn: CreateFn | StreamFn | None, kind: str) -> Any:
        if fn is None:
            raise ReplayMiss(
                f"the recorded run has no match for this request and no {kind}_fn was "
                f"supplied, so there is nothing to call. Pass {kind}_fn to model() to "
                f"let record mode {self._session.mode!r} record the new request."
            )
        return fn

    def create(
        self, *, messages: list[Message], tools: list[str] | None = None, **params: Any
    ) -> Completion:
        decode = DecodeParams(**params)
        messages = self._session._turn_messages(messages)
        hit = self._matched(messages, tools)
        if hit is not None:
            self._session._current_call_id = hit.call_id
            if hit.stream is not None:
                raise ReplayMiss(
                    f"call {hit.call_id} was recorded as a stream but the replayed agent "
                    f"called create(). Use stream() so the agent consumes it the same way "
                    f"it did when recorded."
                )
            return Completion(call_id=hit.call_id, response=hit.response)

        started = real_perf_counter()
        response = self._backend(self._create_fn, "create")(self._model_id, messages, decode)
        call_id = self._session._append_model_call(
            provider=self._provider,
            model_id=self._model_id,
            model_version=self._model_version,
            params=decode,
            tools=tools,
            messages=messages,
            response=response,
            stream=None,
            duration_ms=(real_perf_counter() - started) * 1000.0,
            chunk_offsets_ms=None,
        )
        return Completion(call_id=call_id, response=response)

    def stream(
        self, *, messages: list[Message], tools: list[str] | None = None, **params: Any
    ) -> StreamHandle:
        decode = DecodeParams(**params)
        # Snapshot the request: the event is written when the stream drains, and
        # agents append to their message list as they go. Hashing the mutated
        # list would record a request that was never sent, and matching hashes at
        # call time, so the two paths would disagree.
        messages = self._session._turn_messages(messages)
        hit = self._matched(messages, tools)
        if hit is not None:
            self._session._current_call_id = hit.call_id
            if hit.stream is None:
                raise ReplayMiss(
                    f"call {hit.call_id} was recorded without streaming, so there are no "
                    f"chunk boundaries to re-emit. Synthesizing one would fabricate data "
                    f"the run never produced. Use create(), or re-record with streaming."
                )
            return StreamHandle(
                hit.call_id, iter(hit.stream.chunks), lambda chunks, offsets: hit.response
            )

        call_id = real_uuid4().hex
        started = real_perf_counter()
        source = _GeneratorSource(
            self._backend(self._stream_fn, "stream")(self._model_id, messages, decode)
        )

        def finalize(chunks: list[StreamChunk], offsets: list[float]) -> ModelResponse:
            response = source.response
            if response is None:
                # Backend raised or returned nothing after yielding chunks. Record
                # what was delivered so replay still has the call the agent saw.
                tool_calls: list[ToolCallRequest] = []
                for chunk in chunks:
                    delta = chunk.tool_call_delta
                    if delta is None or "id" not in delta:
                        continue
                    args = delta.get("args")
                    tool_calls.append(
                        ToolCallRequest(
                            id=str(delta["id"]),
                            name=str(delta.get("name", "")),
                            args=args if isinstance(args, dict) else {},
                            batch_index=len(tool_calls),
                        )
                    )
                response = ModelResponse(
                    text="".join(c.text_delta for c in chunks),
                    tool_calls=tool_calls,
                    finish_reason="error",
                    usage=Usage(),
                )
            self._session._append_model_call(
                provider=self._provider,
                model_id=self._model_id,
                model_version=self._model_version,
                params=decode,
                tools=tools,
                messages=messages,
                response=response,
                stream=StreamRecord(chunks=chunks),
                duration_ms=(real_perf_counter() - started) * 1000.0,
                chunk_offsets_ms=offsets,
                call_id=call_id,
            )
            return response

        return StreamHandle(call_id, iter(source), finalize)


class Tools:
    def __init__(self, session: Session, dispatch_fn: DispatchFn | None) -> None:
        self._session = session
        self._dispatch = dispatch_fn

    def call(self, parent_call_id: str, request: ToolCallRequest) -> ToolOutcome:
        token = tool_scope.set((parent_call_id, request.id))
        try:
            return self._call(parent_call_id, request)
        finally:
            tool_scope.reset(token)

    def _call(self, parent_call_id: str, request: ToolCallRequest) -> ToolOutcome:
        # Hash the form that will be stored, exactly as the model call path does.
        # Redaction rewrites argument values on their way to disk, so a hash taken
        # before it and one taken after it would never agree — and a cassette
        # scrubbed on one machine has to match on another, where the raw home
        # paths behind those arguments differ.
        stored_args = self._session._redactor.args(request.args)
        args_hash = hash_args(stored_args)
        # Keyed by (parent, tool id) rather than sequence, so a parallel batch
        # replays correctly no matter what order its calls complete in. A forked
        # session takes none of them: it re-executes so the world reaches the
        # state the trajectory describes.
        recorded = (
            None
            if self._session.forked
            else self._session._tool_calls.get((parent_call_id, request.id))
        )
        if recorded is not None and args_hash == recorded.args_hash:
            self._session.report.tool_calls_replayed += 1
            content = self._session._store.blobs.get(recorded.result.digest).decode("utf-8")
            return ToolOutcome(
                content=content, status=recorded.status, error=recorded.error
            )
        if not self._session.can_record:
            if recorded is None:
                raise ReplayMiss(
                    f"no recorded result for tool {request.name!r} (id {request.id}) under "
                    f"model call {parent_call_id} in run {self._session.run_id}."
                )
            raise ReplayMiss(
                f"tool {request.name!r} (id {request.id}) was called with different "
                f"arguments than recorded: {args_hash[:12]} now vs "
                f"{recorded.args_hash[:12]} in run {self._session.run_id}. The replayed "
                f"agent diverged."
            )

        if self._dispatch is None:
            raise ReplayMiss(
                f"tool {request.name!r} (id {request.id}) has no recorded result and no "
                f"dispatch function was supplied, so there is nothing to call."
            )
        started = real_perf_counter()
        outcome = self._dispatch(request.name, request.args)
        duration = (real_perf_counter() - started) * 1000.0
        self._session._append(
            ToolCallEvent(
                parent_call_id=parent_call_id,
                tool_call_id=request.id,
                batch_index=request.batch_index,
                name=request.name,
                args=stored_args,
                args_hash=args_hash,
                result=self._session._put_blob(outcome.content.encode("utf-8")),
                status=outcome.status,
                error=outcome.error,
                meta=EventMeta(recorded_at=real_time(), duration_ms=duration),
            )
        )
        return outcome


class Clock:
    def __init__(self, session: Session) -> None:
        self._session = session

    def time(self) -> float:
        return float(self._session.env_value("clock", None, real_time))

    def monotonic(self) -> float:
        return float(self._session.env_value("monotonic", None, real_monotonic))


class Session:
    """One recording or replaying agent run.

    Record mode governs what gets written; whether a cassette already exists
    governs what gets read. `once` replays an existing cassette and records a new
    one otherwise, `none` only replays, `new_episodes` replays what matches and
    records what does not, and `all` always records. Collapsing these into a
    single object is what lets a replayed agent that walks off the recorded path
    keep going and capture the new branch instead of hard-failing.
    """

    def __init__(
        self,
        *,
        store: Store,
        header: RunHeader,
        mode: RecordMode,
        config: Config,
        replay_events: list[StoredEvent] | None,
        owns_store: bool = True,
        intervention: Intervention | None = None,
    ) -> None:
        self.run_id = header.run_id
        self.name = header.name
        self.mode = mode
        self.config = config
        self.header = header
        self.intervention = intervention
        self.blocks_dropped = 0
        self._turn = 0
        self._store = store
        self._owns_store = owns_store
        self._redactor = Redactor(config)
        self._current_call_id: str | None = None
        self._models: dict[tuple[str, str, str | None], ModelIdentity] = {}

        self.can_replay = replay_events is not None
        self.can_record = mode == "all" or mode == "new_episodes" or (
            mode == "once" and not self.can_replay
        )
        self.report = ReplayReport(can_record=self.can_record)
        # A fork replays the model and re-executes the world. Serving a recorded
        # tool result would skip its effect on the working tree, so a run that
        # continued past the change would act on a tree the replayed prefix
        # never actually built. Inference is the expensive input and the one the
        # log exists to capture; tool calls are the agent's effect on the world
        # and have to happen for that world to be real.
        self.forked = intervention is not None

        self._tool_calls: dict[tuple[str, str], ToolCallEvent] = {}
        self._env: dict[tuple[str, str | None], list[Any]] = {}
        self._env_cursor: dict[tuple[str, str | None], int] = {}
        self._fs: dict[tuple[str, str], list[FsReadEvent | FsWriteEvent]] = {}
        self._fs_cursor: dict[tuple[str, str], int] = {}
        self._outcome: OutcomeEvent | None = None
        self._matcher: CallMatcher | None = None
        if replay_events is not None:
            self._index(replay_events)
        if self.forked:
            # A different outcome is the result a fork is looking for, so the
            # source run's outcome is not something to check this one against.
            self._outcome = None

        self.clock = Clock(self)
        self.fs = Fs(self)

    def _index(self, events: list[StoredEvent]) -> None:
        calls: list[ModelCallEvent] = []
        for stored in events:
            ev = stored.event
            match ev:
                case ModelCallEvent():
                    calls.append(ev)
                case ToolCallEvent():
                    self._tool_calls[(ev.parent_call_id, ev.tool_call_id)] = ev
                case EnvironmentEvent():
                    self._env.setdefault((ev.source, ev.key), []).append(ev.value)
                case FsReadEvent():
                    self._fs.setdefault((ev.kind, ev.path), []).append(ev)
                case FsWriteEvent():
                    self._fs.setdefault(("write", ev.path), []).append(ev)
                case OutcomeEvent():
                    self._outcome = ev
        self._matcher = CallMatcher(calls, self.config, self.report)

    def _turn_messages(self, messages: list[Message]) -> list[Message]:
        turn = self._turn
        self._turn += 1
        if self.intervention is None or turn < self.intervention.from_turn:
            return list(messages)
        kept = [m for m in messages if m.provenance not in self.intervention.drop_tags]
        self.blocks_dropped += len(messages) - len(kept)
        self.report.blocks_dropped = self.blocks_dropped
        return kept

    def model(
        self,
        *,
        provider: str,
        model_id: str,
        model_version: str | None = None,
        create_fn: CreateFn | None = None,
        stream_fn: StreamFn | None = None,
    ) -> Model:
        # The backend functions are optional so harness code stays byte-identical
        # between record and replay. A pure replay never calls them.
        return Model(self, provider, model_id, model_version, create_fn, stream_fn)

    def tools(self, dispatch_fn: DispatchFn | None = None) -> Tools:
        return Tools(self, dispatch_fn)

    def outcome(
        self,
        *,
        status: Literal["ok", "error"],
        error: str | None = None,
        usage: Usage | None = None,
        coverage: bool | None = None,
        resolve: bool | None = None,
        patch: str | None = None,
        test_summary: str | None = None,
    ) -> None:
        if self._outcome is not None:
            if status != self._outcome.status:
                raise ReplayMiss(
                    f"replayed run ended {status!r} but was recorded as "
                    f"{self._outcome.status!r}. The replayed agent diverged."
                )
            if not self.can_record:
                return
            # new_episodes may replace outcome details (patch, coverage) on an
            # extended run that still ends with the same status.
        elif not self.can_record:
            raise ReplayMiss(f"run {self.run_id} has no recorded outcome to replay against.")
        event = OutcomeEvent(
            status=status,
            error=error,
            usage=usage or Usage(),
            coverage=coverage,
            resolve=resolve,
            patch=self._put_blob(patch.encode("utf-8")) if patch is not None else None,
            test_summary=test_summary,
            parent_call_id=self._current_call_id,
            meta=EventMeta(recorded_at=real_time()),
        )
        self._append(event)
        self._outcome = event

    def env_value(self, source: str, key: str | None, produce: Callable[[], Any]) -> Any:
        if self.can_replay and not self.forked:
            found, value = self._pop_env(source, key)
            if found:
                return self._redactor.restore_path(value) if source == "env" else value
        if not self.can_record:
            self._miss(
                f"run {self.run_id} has no unconsumed {source!r} value"
                f"{f' for {key!r}' if key else ''} left. The replayed agent read it more "
                f"times than the recorded run did."
            )
        value = produce()
        self._append(
            EnvironmentEvent(
                source=source,
                key=key,
                value=value,
                parent_call_id=self._current_call_id,
                tool_call_id=tool_scope.get()[1],
                meta=EventMeta(recorded_at=real_time()),
            )
        )
        return value

    def _pop_env(self, source: str, key: str | None) -> tuple[bool, Any]:
        values = self._env.get((source, key))
        if not values:
            return (False, None)
        index = self._env_cursor.get((source, key), 0)
        if index < len(values):
            self._env_cursor[(source, key)] = index + 1
            return (True, values[index])
        # A variable is looked up by name, not consumed from a sequence, and how
        # many times library code reads one varies between runs. Re-reading a
        # variable the run did record is not divergence; reading one it never
        # recorded is, and that still misses above.
        if source == "env":
            return (True, values[-1])
        return (False, None)

    def _replay_fs(self, kind: str, path: str) -> Any:
        # A fork reads the tree it is actually building. Serving a recorded read
        # would hand back the file as it was before this run's own edits.
        if not self.can_replay or self.forked:
            return None
        entries = self._fs.get((kind, path))
        index = self._fs_cursor.get((kind, path), 0)
        if entries and index < len(entries):
            self._fs_cursor[(kind, path)] = index + 1
            return entries[index]
        # Both ways of running out are divergence, and a replay that cannot record
        # has to say so rather than fall through: falling through would read the
        # live filesystem and hand the agent content the cassette never held.
        if not self.can_record:
            if not entries:
                self._miss(
                    f"the replayed agent accessed {path!r} ({kind}), which the recorded run "
                    f"never accessed. The replayed agent diverged. Record with mode "
                    f"'new_episodes' to capture where it goes instead."
                )
            self._miss(
                f"the replayed agent accessed {path!r} more times than the recorded run "
                f"did ({len(entries)} recorded {kind} accesses). The replayed agent "
                f"diverged."
            )
        return None

    def _fs_key(self, path: str | Path) -> str:
        return self._redactor.text(str(path))

    def _scope_fields(self) -> dict[str, Any]:
        parent, tool_call_id = tool_scope.get()
        return {
            "parent_call_id": parent or self._current_call_id,
            "tool_call_id": tool_call_id,
            "meta": EventMeta(recorded_at=real_time()),
        }

    def _miss(self, message: str) -> NoReturn:
        raise ReplayMiss(message)

    def _saw_model(self, identity: ModelIdentity) -> None:
        self._models[(identity.provider, identity.model_id, identity.model_version)] = identity

    @property
    def models(self) -> list[ModelIdentity]:
        return list(self._models.values())

    def _put_blob(self, data: bytes) -> BlobRef:
        return self._store.blobs.put(self._redactor.blob(data))

    def _append(self, event: AnyEvent) -> int | None:
        # A replay reads its own run, so an append during one would rewrite the
        # recording being measured against. Every path that reaches here is meant
        # to have checked already; this is the backstop, because the failure is
        # silent corruption of a cassette rather than a visible error.
        if not self.can_record:
            raise ReplayMiss(
                f"a {event.type!r} event was recorded while replaying run {self.run_id}, "
                f"which is read-only. Record mode {self.mode!r} cannot write. Use "
                f"'new_episodes' to capture what a divergent replay does."
            )
        redacted = self._redactor.event(event)
        if redacted is None:
            return None
        return self._store.append(self.run_id, redacted)

    def _append_model_call(
        self,
        *,
        provider: str,
        model_id: str,
        model_version: str | None,
        params: DecodeParams,
        tools: list[str] | None,
        messages: list[Message],
        response: ModelResponse,
        stream: StreamRecord | None,
        duration_ms: float,
        chunk_offsets_ms: list[float] | None,
        call_id: str | None = None,
    ) -> str:
        call_id = call_id or real_uuid4().hex
        # Hash the form that will be stored. The backend was handed the original
        # messages, but a cassette holds the redacted ones, and replay hashes
        # what it is given after redacting it too.
        stored_messages = self._redactor.messages(messages)
        self._append(
            ModelCallEvent(
                call_id=call_id,
                provider=provider,
                model_id=model_id,
                model_version=model_version,
                params=params,
                tools=tools,
                messages=stored_messages,
                messages_hash=hash_messages(stored_messages),
                response=response,
                stream=stream,
                parent_call_id=self._current_call_id,
                meta=EventMeta(
                    recorded_at=real_time(),
                    duration_ms=duration_ms,
                    chunk_offsets_ms=chunk_offsets_ms,
                ),
            )
        )
        self._current_call_id = call_id
        if self.can_replay:
            self.report.recorded_new += 1
        return call_id


def plan_intervention(
    source: RunHeader,
    events: list[StoredEvent],
    drop_tags: frozenset[str],
    from_turn: int,
) -> Intervention:
    """Check the change can happen before any inference is spent on it.

    An intervention that matches nothing still produces a plausible-looking run,
    and that run is a re-recording rather than a counterfactual. Finding out
    afterward means the answer is wrong in a way nothing on the page shows.
    """
    if not drop_tags:
        raise ValueError("an intervention has to drop at least one provenance tag.")
    if from_turn < 0:
        raise ValueError(f"from_turn cannot be negative (got {from_turn}).")
    calls = [e.event for e in events if isinstance(e.event, ModelCallEvent)]
    if from_turn >= len(calls):
        raise ValueError(
            f"run {source.run_id[:12]} made {len(calls)} model calls, so it has no turn "
            f"{from_turn} to intervene at. Turns are numbered from 0."
        )
    hits = sum(
        1 for call in calls[from_turn:] for m in call.messages if m.provenance in drop_tags
    )
    if not hits:
        available = sorted({m.provenance for c in calls for m in c.messages if m.provenance})
        raise ValueError(
            f"no context block tagged {', '.join(sorted(drop_tags))} appears at or after "
            f"turn {from_turn} of run {source.run_id[:12]}, so dropping it would change "
            f"nothing. Tags in this run: {', '.join(available) or 'none'}."
        )
    return Intervention(
        source_run_id=source.run_id,
        drop_tags=drop_tags,
        from_turn=from_turn,
        blocks=hits,
    )


def warn_if_stale(header: RunHeader, config: Config) -> str | None:
    if config.stale_after_days is None:
        return None
    age_days = (real_time() - header.started_at) / SECONDS_PER_DAY
    if age_days < config.stale_after_days:
        return None
    models = ", ".join(f"{m.provider}/{m.model_id}" for m in header.models) or "unknown"
    message = (
        f"cassette {header.name!r} was recorded {age_days:.0f} days ago against {models}. "
        f"Model weights change under a stable model id, so this may be replaying a model "
        f"that no longer behaves the way it did when recorded. Re-record to be sure."
    )
    warnings.warn(message, CassetteStale, stacklevel=3)
    return message
