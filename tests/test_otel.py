"""A recorded run exported as OTLP/JSON under the GenAI semantic conventions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import tracewake
from tracewake import Store
from tracewake.otel import build_spans

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


def _spans(tmp_path: Path, run_id: str) -> list[dict]:
    db = Store(tmp_path)
    payload = build_spans(db.run(run_id), db.events(run_id))
    db.close()
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span: dict) -> dict:
    out = {}
    for pair in span["attributes"]:
        (kind, value), = pair["value"].items()
        out[pair["key"]] = value if kind != "arrayValue" else [
            list(v.values())[0] for v in value["values"]
        ]
    return out


def test_every_model_call_becomes_a_gen_ai_chat_span(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    spans = _spans(tmp_path, run_id)

    chats = [s for s in spans if _attrs(s).get("gen_ai.operation.name") == "chat"]
    tools = [s for s in spans if _attrs(s).get("gen_ai.operation.name") == "execute_tool"]
    assert len(chats) == 3
    assert len(tools) == 5
    for span in chats:
        attrs = _attrs(span)
        assert attrs["gen_ai.system"] == "mock"
        assert attrs["gen_ai.request.model"] == "mock-1"
        assert attrs["gen_ai.response.finish_reasons"]


def test_span_ids_are_well_formed_and_tool_spans_hang_off_their_call(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    spans = _spans(tmp_path, run_id)

    ids = {s["spanId"] for s in spans}
    assert len(ids) == len(spans)
    assert {len(s["traceId"]) for s in spans} == {32}
    assert {len(s["spanId"]) for s in spans} == {16}
    assert len({s["traceId"] for s in spans}) == 1

    root = [s for s in spans if "parentSpanId" not in s]
    assert len(root) == 1
    # Every other span resolves to a parent inside the same trace, which is what
    # makes the export a tree rather than a bag of spans.
    assert all(s["parentSpanId"] in ids for s in spans if s is not root[0])


def test_exporting_the_same_run_twice_produces_the_same_trace(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    assert _spans(tmp_path, run_id) == _spans(tmp_path, run_id)


def test_usage_on_the_spans_equals_the_recorded_usage(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    spans = _spans(tmp_path, run_id)

    db = Store(tmp_path)
    events = [e.event for e in db.events(run_id) if e.event.type == "model_call"]
    db.close()

    exported = sum(
        int(_attrs(s)["gen_ai.usage.input_tokens"])
        for s in spans
        if _attrs(s).get("gen_ai.operation.name") == "chat"
    )
    assert exported == sum(e.response.usage.input_tokens for e in events)


def test_a_failed_tool_call_carries_an_error_status(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    spans = _spans(tmp_path, run_id)

    failed = [s for s in spans if _attrs(s).get("gen_ai.tool.name") == "run_tests"]
    assert failed and failed[0]["status"]["code"] == 2
    assert failed[0]["status"]["message"] == "pytest exited 1"


def test_the_cli_writes_a_parseable_otlp_file(tmp_path: Path):
    run_id, _ = _record(tmp_path)
    out = tmp_path / "trace.json"
    done = subprocess.run(
        [sys.executable, "-m", "tracewake", "otel", run_id, "-o", str(out), "--store", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] == "tracewake"
    assert "spans" in done.stdout
