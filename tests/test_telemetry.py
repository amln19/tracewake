from __future__ import annotations

import io
import json
import subprocess
import sys
from typing import Any

import pytest

from locus.telemetry import Telemetry

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

SPAN_FIELDS = {
    "telemetry",
    "scope",
    "service_name",
    "deployment_environment",
    "trace_id",
    "span_id",
    "name",
    "kind",
    "start_time",
    "end_time",
    "duration_ms",
    "status",
    "resource",
}


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture
def recorder() -> tuple[Telemetry, io.StringIO]:
    stream = io.StringIO()
    return Telemetry(service="locus-worker", version="0.0.0", environment="test", stream=stream), stream


def test_span_record_carries_the_shared_fields(recorder: tuple[Telemetry, io.StringIO]) -> None:
    telemetry, stream = recorder
    with telemetry.span("worker.execute", kind="consumer") as span:
        span.set(operation="diff")
    (record,) = records(stream)
    assert SPAN_FIELDS <= set(record)
    assert record["telemetry"] == "span"
    assert record["kind"] == "consumer"
    assert record["status"] == "Unset"
    assert record["attributes"] == {"operation": "diff"}
    assert record["end_time"].endswith("Z") and record["duration_ms"] >= 0


def test_span_continues_the_trace_a_notification_carried(recorder: tuple[Telemetry, io.StringIO]) -> None:
    telemetry, stream = recorder
    with telemetry.span("worker.execute", kind="consumer", parent=TRACEPARENT) as span:
        with telemetry.span("worker.download", parent=span):
            pass
    child, parent = records(stream)
    assert parent["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parent["parent_span_id"] == "00f067aa0ba902b7"
    assert child["trace_id"] == parent["trace_id"]
    assert child["parent_span_id"] == parent["span_id"]


def test_a_malformed_traceparent_starts_a_new_trace(recorder: tuple[Telemetry, io.StringIO]) -> None:
    telemetry, stream = recorder
    with telemetry.span("worker.execute", parent="01-not-a-trace"):
        pass
    (record,) = records(stream)
    assert len(record["trace_id"]) == 32
    assert "parent_span_id" not in record


def test_a_failing_span_reports_an_error(recorder: tuple[Telemetry, io.StringIO]) -> None:
    telemetry, stream = recorder
    with pytest.raises(ValueError):
        with telemetry.span("worker.analyze"):
            raise ValueError("analysis failed")
    (record,) = records(stream)
    assert record["status"] == "Error"


def test_metrics_use_embedded_format_with_bounded_dimensions(recorder: tuple[Telemetry, io.StringIO]) -> None:
    telemetry, stream = recorder
    telemetry.job_finished("diff", "succeeded")
    telemetry.stage_finished("a-client-invented-this", "an-invented-stage", 12.5)
    finished, staged = records(stream)
    directive = finished["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "Locus/Worker"
    assert directive["Dimensions"] == [["Operation", "Outcome"]]
    assert directive["Metrics"] == [{"Name": "WorkerJobs", "Unit": "Count"}]
    assert finished["Operation"] == "diff" and finished["Outcome"] == "succeeded"
    assert finished["WorkerJobs"] == 1
    assert staged["Operation"] == "other" and staged["Stage"] == "other"
    assert staged["WorkerStageMillis"] == 12.5


def test_telemetry_can_be_turned_off(recorder: tuple[Telemetry, io.StringIO]) -> None:
    _, stream = recorder
    telemetry = Telemetry(stream=stream, enabled=False)
    with telemetry.span("worker.execute"):
        pass
    telemetry.job_finished("diff", "succeeded")
    assert stream.getvalue() == ""


def test_importing_locus_emits_no_telemetry() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import locus; print('ok')"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "ok\n"
