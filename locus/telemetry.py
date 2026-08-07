"""Operational telemetry for the hosted worker.

This describes the worker process — what it claimed, how long each stage took,
whether it committed. It is unrelated to `locus.otel`, which encodes a recorded
run as spans for the tenant who asked for that artifact.

Records leave as one JSON object per line: OpenTelemetry span records in the
same shape the control plane emits, and CloudWatch embedded metric format for
the counters an alarm watches. Nothing here is needed for local Locus, so the
module depends only on the standard library.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, Any

SCOPE = "locus.worker"
TRACEPARENT = re.compile(r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-[0-9a-f]{2}$")

# Every dimension value comes from one of these sets, so an unexpected value
# collapses rather than creating an unbounded number of metric series.
OPERATIONS = ("validate", "diff", "otlp", "pprof")
OUTCOMES = ("succeeded", "failed", "fenced", "refused")
STAGES = ("download", "analyze", "upload", "commit")
OTHER = "other"


def bounded(value: str, allowed: tuple[str, ...]) -> str:
    return value if value in allowed else OTHER


def _timestamp(nanoseconds: int) -> str:
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{remainder:09d}Z"


@dataclass
class Span:
    name: str
    kind: str
    trace_id: str
    span_id: str
    parent_span_id: str
    started: int
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "Unset"

    def set(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


class Telemetry:
    """Writes span and metric records for one worker process."""

    def __init__(
        self,
        *,
        service: str = "locus-worker",
        version: str = "unknown",
        environment: str = "local",
        namespace: str = "Locus/Worker",
        stream: IO[str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.service = service
        self.version = version
        self.environment = environment
        self.namespace = namespace
        self.enabled = enabled
        self._stream = stream if stream is not None else sys.stdout

    @classmethod
    def from_environment(cls, stream: IO[str] | None = None) -> Telemetry:
        return cls(
            version=os.environ.get("LOCUS_SERVICE_VERSION", "unknown"),
            environment=os.environ.get("LOCUS_ENVIRONMENT", "local"),
            namespace=os.environ.get("LOCUS_WORKER_METRIC_NAMESPACE", "Locus/Worker"),
            stream=stream,
            enabled=os.environ.get("LOCUS_TELEMETRY", "").lower() != "off",
        )

    def _write(self, record: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        self._stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
        self._stream.flush()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        parent: Span | str | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        span = self._begin(name, kind, parent, attributes)
        try:
            yield span
        except BaseException:
            span.status = "Error"
            self._end(span)
            raise
        self._end(span)

    def _begin(self, name: str, kind: str, parent: Span | str | None, attributes: dict[str, Any]) -> Span:
        trace_id, parent_span = "", ""
        if isinstance(parent, Span):
            trace_id, parent_span = parent.trace_id, parent.span_id
        elif isinstance(parent, str) and (match := TRACEPARENT.match(parent)):
            trace_id, parent_span = match["trace"], match["span"]
        return Span(
            name=name,
            kind=kind,
            trace_id=trace_id or os.urandom(16).hex(),
            span_id=os.urandom(8).hex(),
            parent_span_id=parent_span,
            started=time.time_ns(),
            attributes=dict(attributes),
        )

    def _end(self, span: Span) -> None:
        finished = time.time_ns()
        record: dict[str, Any] = {
            "telemetry": "span",
            "scope": SCOPE,
            "service_name": self.service,
            "deployment_environment": self.environment,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "name": span.name,
            "kind": span.kind,
            "start_time": _timestamp(span.started),
            "end_time": _timestamp(finished),
            "duration_ms": (finished - span.started) / 1_000_000,
            "status": span.status,
            "resource": {"service.version": self.version},
        }
        if span.parent_span_id:
            record["parent_span_id"] = span.parent_span_id
        if span.attributes:
            record["attributes"] = span.attributes
        self._write(record)

    def metric(self, values: Mapping[str, tuple[Any, str]], dimensions: Mapping[str, str]) -> None:
        """Emit one embedded-metric record: {name: (value, unit)} plus its dimensions."""
        record: dict[str, Any] = {
            "_aws": {
                "Timestamp": time.time_ns() // 1_000_000,
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [sorted(dimensions)],
                        "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in sorted(values.items())],
                    }
                ],
            },
            "telemetry": "metric",
            "service_name": self.service,
            "deployment_environment": self.environment,
        }
        record.update(dimensions)
        record.update({name: value for name, (value, _) in values.items()})
        self._write(record)

    def job_finished(self, operation: str, outcome: str) -> None:
        self.metric(
            {"WorkerJobs": (1, "Count")},
            {"Operation": bounded(operation, OPERATIONS), "Outcome": bounded(outcome, OUTCOMES)},
        )

    def stage_finished(self, operation: str, stage: str, milliseconds: float) -> None:
        self.metric(
            {"WorkerStageMillis": (round(milliseconds, 3), "Milliseconds")},
            {"Operation": bounded(operation, OPERATIONS), "Stage": bounded(stage, STAGES)},
        )
