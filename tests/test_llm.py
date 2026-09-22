from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tracewake
from tracewake import (
    DecodeParams,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    ModelResponse,
    OpenAICompatibleProvider,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
    build_localization_evidence,
    load_prompt,
    localize_with_llm,
)
from tracewake.align import extract_steps
from tracewake.cli import app
from tracewake.diverge import localize


def _record(store: Path, name: str, tool_name: str = "inspect") -> str:
    def create(
        model_id: str, messages: list[Message], params: DecodeParams
    ) -> ModelResponse:
        return ModelResponse(
            text=f"reasoning from {name}",
            tool_calls=[
                ToolCallRequest(
                    id=f"tool-{name}",
                    name=tool_name,
                    args={"path": f"{name}.py"},
                    batch_index=0,
                )
            ],
            finish_reason="tool_use",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    with tracewake.record(name, store=store) as session:
        model = session.model(provider="fixture", model_id="fixture-1", create_fn=create)
        call = model.create(messages=[Message(role="user", content="find the failure")])
        session.tools(lambda _name, _args: ToolOutcome(content=f"observation from {name}")) \
            .call(call.call_id, call.response.tool_calls[0])
        session.outcome(status="error", error="failed")
        return session.run_id


class FakeProvider:
    name = "fake"
    endpoint = "memory://fake"

    def __init__(self, decision: dict[str, Any]) -> None:
        self.decision = decision
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest, *, timeout_seconds: float) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(self.decision),
            model="fake-reported",
            request_id="request-1",
        )


def test_llm_localization_sends_structured_recorded_evidence(tmp_path: Path) -> None:
    run_id = _record(tmp_path, "bad")
    store = Store(tmp_path)
    events = store.events(run_id)
    steps = extract_steps(events)
    step, reliability = localize(steps)
    provider = FakeProvider(
        {
            "suggested_step": 1,
            "confidence": "moderate",
            "evidence_steps": [1],
            "abstain": False,
            "explanation": "The observation shows the attempted path did not resolve the task.",
        }
    )

    result = localize_with_llm(
        provider,
        "fake-requested",
        events,
        store.blobs,
        deterministic_step=step,
        reliability=reliability,
    )
    store.close()

    assert result.authoritative is False
    assert result.deterministic.step == step
    assert result.advisory.suggested_step == 1
    assert result.provenance.requested_model == "fake-requested"
    assert result.provenance.reported_model == "fake-reported"
    request = provider.requests[0]
    evidence_message = request.messages[1].content
    assert "observation from bad" in evidence_message
    assert "reasoning from bad" in evidence_message
    assert "find the failure" in evidence_message
    assert '"status":"error"' in evidence_message
    assert '"profile":"localize-v1"' in evidence_message


def test_llm_localization_rejects_out_of_evidence_steps(tmp_path: Path) -> None:
    run_id = _record(tmp_path, "bad")
    store = Store(tmp_path)
    events = store.events(run_id)
    provider = FakeProvider(
        {
            "suggested_step": 2,
            "confidence": "high",
            "evidence_steps": [2],
            "abstain": False,
            "explanation": "Invented a step.",
        }
    )

    with pytest.raises(LLMError, match="not included"):
        localize_with_llm(
            provider,
            "fake",
            events,
            store.blobs,
            deterministic_step=1,
            reliability="silent-short",
        )
    store.close()


def test_llm_localization_strictly_rejects_coerced_step_numbers(
    tmp_path: Path,
) -> None:
    run_id = _record(tmp_path, "bad")
    store = Store(tmp_path)
    events = store.events(run_id)
    provider = FakeProvider(
        {
            "suggested_step": "1",
            "confidence": "high",
            "evidence_steps": ["1"],
            "abstain": False,
            "explanation": "The step numbers have the wrong JSON types.",
        }
    )

    with pytest.raises(LLMError, match="does not match"):
        localize_with_llm(
            provider,
            "fake",
            events,
            store.blobs,
            deterministic_step=1,
            reliability="silent-short",
        )
    store.close()


def test_llm_evidence_rejects_a_zero_deterministic_step(tmp_path: Path) -> None:
    run_id = _record(tmp_path, "bad")
    store = Store(tmp_path)

    with pytest.raises(LLMError, match="must be one-based"):
        build_localization_evidence(
            store.events(run_id),
            store.blobs,
            deterministic_step=0,
            reliability="silent-short",
        )
    store.close()


def _serve_llm(
    decision: dict[str, Any],
) -> tuple[HTTPServer, str, list[dict[str, Any]], list[str | None]]:
    requests: list[dict[str, Any]] = []
    authorizations: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(size)))
            authorizations.append(self.headers.get("Authorization"))
            response = json.dumps(
                {
                    "id": "completion-1",
                    "model": "served-model",
                    "choices": [{"message": {"content": json.dumps(decision)}}],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "total_tokens": 30,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1", requests, authorizations


def _decision(step: int = 1) -> dict[str, Any]:
    return {
        "suggested_step": step,
        "confidence": "moderate",
        "evidence_steps": [step],
        "abstain": False,
        "explanation": "This is the earliest supported failure step.",
    }


def test_compatible_provider_uses_chat_completions_without_leaking_key() -> None:
    server, base_url, requests, authorizations = _serve_llm(_decision())
    try:
        provider = OpenAICompatibleProvider(base_url, api_key="secret-key")
        response = provider.complete(
            LLMRequest(
                model="requested-model",
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "user"},
                ],
            ),
            timeout_seconds=5,
        )
    finally:
        server.shutdown()

    assert provider.endpoint.endswith("/v1/chat/completions")
    assert authorizations == ["Bearer secret-key"]
    assert requests[0]["model"] == "requested-model"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert response.model == "served-model"
    assert response.usage.total_tokens == 30
    assert "secret-key" not in response.raw_sha256


def test_compatible_provider_enforces_a_total_response_deadline() -> None:
    response_body = json.dumps(
        {
            "choices": [
                {"message": {"content": json.dumps(_decision())}}
            ]
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.end_headers()
            try:
                for byte in response_body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = OpenAICompatibleProvider(
        f"http://127.0.0.1:{server.server_port}/v1"
    )
    started = time.monotonic()
    try:
        with pytest.raises(LLMError, match="exceeded 0.05 seconds"):
            provider.complete(
                LLMRequest(
                    model="requested-model",
                    messages=[
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "user"},
                    ],
                ),
                timeout_seconds=0.05,
            )
    finally:
        server.shutdown()

    assert time.monotonic() - started < 0.5


def test_plain_http_requires_loopback_or_an_explicit_override() -> None:
    with pytest.raises(LLMError, match="plain HTTP"):
        OpenAICompatibleProvider("http://models.internal/v1")
    provider = OpenAICompatibleProvider(
        "http://models.internal/v1", allow_insecure_http=True
    )
    assert provider.endpoint == "http://models.internal/v1/chat/completions"


@pytest.mark.parametrize(
    "base_url",
    ["http://[::1", "http://localhost:not-a-port/v1"],
)
def test_malformed_endpoint_is_a_domain_error(base_url: str) -> None:
    with pytest.raises(LLMError, match="valid absolute"):
        OpenAICompatibleProvider(base_url)


def test_missing_custom_prompt_is_a_domain_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing-prompt.txt"

    with pytest.raises(LLMError, match="could not read LLM prompt file"):
        load_prompt(missing)


def test_localize_llm_cli_writes_a_validated_advisory(tmp_path: Path) -> None:
    store_path = tmp_path / "store"
    run_id = _record(store_path, "bad")
    server, base_url, requests, _ = _serve_llm(_decision())
    output = tmp_path / "advice.json"
    try:
        result = CliRunner().invoke(
            app,
            [
                "localize",
                run_id,
                "--store",
                str(store_path),
                "--llm",
                "--llm-json",
                str(output),
            ],
            env={
                "TRACEWAKE_LLM_PROVIDER": "compatible",
                "TRACEWAKE_LLM_MODEL": "fixture-model",
                "TRACEWAKE_LLM_BASE_URL": base_url,
                "TRACEWAKE_LLM_API_KEY": "fixture-key",
            },
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert "first irrecoverable step" in result.output
    assert "LLM advisory (not authoritative)" in result.output
    assert len(requests) == 1
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["authoritative"] is False
    assert artifact["advisory"]["suggested_step"] == 1
    assert "fixture-key" not in output.read_text(encoding="utf-8")


def test_diff_llm_cli_supplies_the_reference_run(tmp_path: Path) -> None:
    store_path = tmp_path / "store"
    good = _record(store_path, "good", "read_file")
    bad = _record(store_path, "bad", "search")
    server, base_url, requests, _ = _serve_llm(_decision())
    try:
        result = CliRunner().invoke(
            app,
            [
                "diff",
                good,
                bad,
                "--store",
                str(store_path),
                "--lexical",
                "--llm",
            ],
            env={
                "TRACEWAKE_LLM_MODEL": "fixture-model",
                "TRACEWAKE_LLM_BASE_URL": base_url,
            },
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert "LLM advisory (not authoritative)" in result.output
    evidence = requests[0]["messages"][1]["content"]
    assert '"mode":"reference-assisted"' in evidence
    assert '"reference_steps"' in evidence


def test_llm_configuration_does_nothing_without_the_flag(tmp_path: Path) -> None:
    store_path = tmp_path / "store"
    run_id = _record(store_path, "bad")
    server, base_url, requests, _ = _serve_llm(_decision())
    try:
        result = CliRunner().invoke(
            app,
            ["localize", run_id, "--store", str(store_path)],
            env={
                "TRACEWAKE_LLM_MODEL": "fixture-model",
                "TRACEWAKE_LLM_BASE_URL": base_url,
                "TRACEWAKE_LLM_API_KEY": "fixture-key",
            },
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert requests == []
    assert "LLM advisory" not in result.output


def test_cli_refuses_to_send_an_unredacted_run(tmp_path: Path) -> None:
    store_path = tmp_path / "store"
    with tracewake.record("raw", store=store_path, redact=False) as session:
        model = session.model(
            provider="fixture",
            model_id="fixture-1",
            create_fn=lambda _model, _messages, _params: ModelResponse(
                text="reason",
                tool_calls=[
                    ToolCallRequest(
                        id="tool", name="inspect", args={"path": "raw.py"}, batch_index=0
                    )
                ],
                finish_reason="tool_use",
            ),
        )
        call = model.create(messages=[Message(role="user", content="go")])
        session.tools(lambda _name, _args: ToolOutcome(content="raw content")).call(
            call.call_id, call.response.tool_calls[0]
        )
        session.outcome(status="error")
        run_id = session.run_id

    result = CliRunner().invoke(
        app,
        ["localize", run_id, "--store", str(store_path), "--llm"],
        env={
            "TRACEWAKE_LLM_MODEL": "fixture-model",
            "TRACEWAKE_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        },
    )

    assert result.exit_code != 0
    assert "refusing to send unredacted" in result.output
