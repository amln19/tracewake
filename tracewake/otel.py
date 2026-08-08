"""A recorded run as OTLP/JSON spans under the GenAI semantic conventions.

The log already holds what a tracing exporter would have emitted live — model,
decode params, finish reason, token usage, tool names and durations — so a run
recorded once can also be read by tooling that speaks OpenTelemetry.

Spans are written in OTLP/JSON rather than through the OpenTelemetry SDK, which
keeps the wheel at two dependencies. Ids are derived from the run and call ids
by hash, so exporting the same run twice produces the same trace rather than a
new one each time.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .events import (
    ModelCallEvent,
    RunHeader,
    StoredEvent,
    ToolCallEvent,
    sha256_hex,
)

SCOPE = "tracewake"
SPAN_KIND_CLIENT = 3
SPAN_KIND_INTERNAL = 1
STATUS_OK = 1
STATUS_ERROR = 2


def _trace_id(run_id: str) -> str:
    return sha256_hex(f"trace:{run_id}".encode())[:32]


def _span_id(run_id: str, key: str) -> str:
    return sha256_hex(f"span:{run_id}:{key}".encode())[:16]


def _value(value: Any) -> dict[str, Any]:
    match value:
        case bool():
            return {"boolValue": value}
        case int():
            return {"intValue": str(value)}
        case float():
            return {"doubleValue": value}
        case list() | tuple():
            return {"arrayValue": {"values": [_value(v) for v in value]}}
        case _:
            return {"stringValue": str(value)}


def _attributes(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in pairs.items() if v is not None]


def _nanos(seconds: float) -> str:
    return str(int(seconds * 1_000_000_000))


def _span(
    *,
    run_id: str,
    key: str,
    parent: str | None,
    name: str,
    kind: int,
    start: float,
    end: float,
    attributes: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    span = {
        "traceId": _trace_id(run_id),
        "spanId": _span_id(run_id, key),
        "name": name,
        "kind": kind,
        "startTimeUnixNano": _nanos(start),
        "endTimeUnixNano": _nanos(end),
        "attributes": _attributes(attributes),
        "status": {"code": STATUS_ERROR if error else STATUS_OK},
    }
    if parent is not None:
        span["parentSpanId"] = _span_id(run_id, parent)
    if error:
        span["status"]["message"] = error
    return span


def build_spans(header: RunHeader, events: Sequence[StoredEvent]) -> dict[str, Any]:
    """One trace per run: a root span, a span per model call, one per tool call."""
    run_id = header.run_id
    spans: list[dict[str, Any]] = []
    ends: list[float] = []

    for stored in events:
        event = stored.event
        # Durations are recorded; start times are not, so a span's start is its
        # completion less how long it took.
        elapsed = (event.meta.duration_ms or 0.0) / 1000.0
        end = event.meta.recorded_at
        start = end - elapsed
        ends.append(end)

        if isinstance(event, ModelCallEvent):
            spans.append(
                _span(
                    run_id=run_id,
                    key=event.call_id,
                    parent="run",
                    name=f"chat {event.model_id}",
                    kind=SPAN_KIND_CLIENT,
                    start=start,
                    end=end,
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.system": event.provider,
                        "gen_ai.request.model": event.model_id,
                        "gen_ai.request.temperature": event.params.temperature,
                        "gen_ai.request.top_p": event.params.top_p,
                        "gen_ai.request.max_tokens": event.params.max_tokens,
                        "gen_ai.request.seed": event.params.seed,
                        "gen_ai.response.finish_reasons": [event.response.finish_reason],
                        "gen_ai.usage.input_tokens": event.response.usage.input_tokens,
                        "gen_ai.usage.output_tokens": event.response.usage.output_tokens,
                        "tracewake.messages_hash": event.messages_hash,
                        "tracewake.run_id": run_id,
                    },
                )
            )
        elif isinstance(event, ToolCallEvent):
            spans.append(
                _span(
                    run_id=run_id,
                    key=f"{event.parent_call_id}/{event.tool_call_id}",
                    parent=event.parent_call_id,
                    name=f"execute_tool {event.name}",
                    kind=SPAN_KIND_INTERNAL,
                    start=start,
                    end=end,
                    attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": event.name,
                        "gen_ai.tool.call.id": event.tool_call_id,
                        "tracewake.tool.batch_index": event.batch_index,
                        "tracewake.tool.result_bytes": event.result.size,
                    },
                    error=event.error,
                )
            )

    root_start = header.started_at
    root_end = header.finished_at or (max(ends) if ends else header.started_at)
    spans.insert(
        0,
        _span(
            run_id=run_id,
            key="run",
            parent=None,
            name=f"agent run {header.name}",
            kind=SPAN_KIND_INTERNAL,
            start=root_start,
            end=root_end,
            attributes={
                "tracewake.run_id": run_id,
                "tracewake.cassette": header.name,
                "tracewake.task_id": header.task_id,
                "gen_ai.request.model": header.models[0].model_id if header.models else None,
                "gen_ai.system": header.models[0].provider if header.models else None,
            },
            error=None if header.status != "error" else "run ended in error",
        ),
    )

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attributes(
                        {"service.name": SCOPE, "tracewake.run_id": run_id}
                    )
                },
                "scopeSpans": [{"scope": {"name": SCOPE}, "spans": spans}],
            }
        ]
    }


def encode_spans(header: RunHeader, events: Sequence[StoredEvent]) -> tuple[bytes, int]:
    payload = build_spans(header, events)
    document = json.dumps(payload, indent=2).encode("utf-8")
    return document, len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])


def write_spans(path: Path, header: RunHeader, events: Sequence[StoredEvent]) -> int:
    document, count = encode_spans(header, events)
    path.write_bytes(document)
    return count
