from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .events import Message, ModelCallEvent, hash_messages, sha256_hex


@dataclass(frozen=True)
class Request:
    model_id: str
    messages_hash: str
    system_hash: str
    tool_names: tuple[str, ...] | None


@dataclass
class ReplayReport:
    """Matched / degraded / miss counts for one replay.

    A diagnostic, not just plumbing: `degraded` counts calls that were matched
    without `messages_hash` among the matchers, so the request was accepted
    without proving it is the request that was recorded.

    `can_record` travels with the counts so the CLI parent knows whether the
    child owned the run. A pure replay must not rewrite the recording. When the
    report is absent the child died before atexit; the parent then finishes
    only a still-running row, never an already-finished cassette.
    """

    recorded_calls: int = 0
    matched: int = 0
    degraded: int = 0
    missed: int = 0
    recorded_new: int = 0
    tool_calls_replayed: int = 0
    misses: list[str] = field(default_factory=list)
    can_record: bool = True
    # Set on forks: how many provenance-tagged messages the intervention removed.
    blocks_dropped: int = 0

    @property
    def unconsumed(self) -> int:
        return self.recorded_calls - self.matched - self.degraded

    def summary(self) -> str:
        parts = [
            f"{self.matched} matched",
            f"{self.degraded} degraded",
            f"{self.missed} missed",
        ]
        if self.recorded_new:
            parts.append(f"{self.recorded_new} newly recorded")
        if self.unconsumed:
            plural = "" if self.unconsumed == 1 else "s"
            parts.append(f"{self.unconsumed} recorded call{plural} unused")
        if self.blocks_dropped:
            unit = "block" if self.blocks_dropped == 1 else "blocks"
            parts.append(f"{self.blocks_dropped} {unit} dropped")
        return ", ".join(parts)


def _system_hash(messages: list[Message]) -> str:
    system = [m for m in messages if m.role == "system"]
    return sha256_hex("".join(m.content for m in system).encode("utf-8"))


def build_request(
    model_id: str, messages: list[Message], tools: list[str] | None
) -> Request:
    return Request(
        model_id=model_id,
        messages_hash=hash_messages(messages),
        system_hash=_system_hash(messages),
        tool_names=tuple(tools) if tools is not None else None,
    )


def _model(recorded: ModelCallEvent, request: Request) -> bool:
    return recorded.model_id == request.model_id


def _messages_hash(recorded: ModelCallEvent, request: Request) -> bool:
    return recorded.messages_hash == request.messages_hash


def _system_prompt(recorded: ModelCallEvent, request: Request) -> bool:
    return _system_hash(recorded.messages) == request.system_hash


def _tool_names(recorded: ModelCallEvent, request: Request) -> bool:
    recorded_names = tuple(recorded.tools) if recorded.tools is not None else None
    return recorded_names == request.tool_names


def _ordinal(recorded: ModelCallEvent, request: Request) -> bool:
    # Ordinal is opt-in rather than a fallback, and it constrains nothing on its
    # own: the scan already takes the first unconsumed call in recorded order, so
    # ordinal is the statement that sequence position alone is enough. A silent
    # fallback to it would mask exactly the divergence this exists to surface.
    return True


PREDICATES = {
    "model": _model,
    "messages_hash": _messages_hash,
    "system_prompt": _system_prompt,
    "tool_names": _tool_names,
    "ordinal": _ordinal,
}


class CallMatcher:
    def __init__(self, calls: list[ModelCallEvent], config: Config, report: ReplayReport) -> None:
        self._calls = calls
        self._consumed = [False] * len(calls)
        self._predicates = [PREDICATES[name] for name in config.match_on]
        self._proves_identity = config.proves_request_identity
        self._match_on = config.match_on
        self._report = report
        report.recorded_calls = len(calls)

    def match(self, request: Request) -> ModelCallEvent | None:
        for index, recorded in enumerate(self._calls):
            if self._consumed[index]:
                continue
            if all(p(recorded, request) for p in self._predicates):
                self._consumed[index] = True
                if self._proves_identity:
                    self._report.matched += 1
                else:
                    self._report.degraded += 1
                return recorded
        self._report.missed += 1
        self._report.misses.append(
            f"model={request.model_id!r} messages_hash={request.messages_hash[:12]}"
        )
        return None

    def describe_miss(self, request: Request, run_id: str) -> str:
        remaining = sum(1 for c in self._consumed if not c)
        return (
            f"no unconsumed model call in run {run_id} matching "
            f"{' + '.join(self._match_on)} for model={request.model_id!r} "
            f"messages_hash={request.messages_hash[:12]} ({remaining} of "
            f"{len(self._calls)} recorded calls still unconsumed). The replayed agent "
            f"built a request the recorded run never made."
        )
