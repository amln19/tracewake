"""Hosted analyses of a bundle against the same analyses run locally.

The worker reaches Locus through the Python APIs, so these tests replace only
the control plane and the object store: the semantics under test are the ones
the CLI uses.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

import locus
from locus import worker
from locus import (
    DecodeParams,
    Message,
    ModelResponse,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)
from locus.align import LexicalEmbedder, diff_runs
from locus.bundle import build_bundle, bundle_header, validate_bundle
from locus.cassette import export_cassette
from locus.otel import build_spans, encode_spans
from locus.pprof import (
    attribute_tokens,
    build_token_profile,
    gzip_profile,
    read_gzipped_profile,
    sample_totals,
    usage_totals,
)
from locus.worker import UnsupportedAnalysis, WorkerClient, run_once

JOB_ID = "00000000-0000-4000-8000-000000000002"
WORKER_ID = "00000000-0000-4000-8000-000000000001"
RUN_IDS = ("018f7f28-df62-7bc4-9f45-6e6c32a19484", "018f7f28-df62-7bc4-9f45-6e6c32a19488")


def _record(store: Path, *, second_tool: str) -> str:
    turn = 0
    script = [
        ("Reading the helper.", "read_file", {"path": "a.py"}, "file contents " * 40, Usage(input_tokens=100, output_tokens=10)),
        ("Changing it.", second_tool, {"path": "a.py"}, "done", Usage(input_tokens=250, output_tokens=20)),
    ]

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        nonlocal turn
        reasoning, tool, args, _, usage = script[turn]
        response = ModelResponse(
            text=reasoning,
            tool_calls=[ToolCallRequest(id=f"t{turn}", name=tool, args=args, batch_index=0)],
            finish_reason="tool_use",
            usage=usage,
        )
        turn += 1
        return response

    def dispatch(name: str, args: dict) -> ToolOutcome:
        for _, tool, tool_args, content, _ in script:
            if tool == name and tool_args == args:
                return ToolOutcome(content=content)
        raise AssertionError(f"unexpected tool {name} {args}")

    with locus.record(second_tool, store=store, task_id="win-off_by_one-1") as rec:
        model = rec.model(provider="acme", model_id="acme-1", create_fn=create)
        tools = rec.tools(dispatch_fn=dispatch)
        messages = [
            Message(role="system", content="sys " * 20, provenance="system_prompt"),
            Message(role="user", content="fix the bug", provenance="task_issue"),
        ]
        for _ in script:
            call = model.create(messages=messages)
            messages.append(Message(role="assistant", content=call.response.text, provenance="assistant_reasoning"))
            for request in call.response.tool_calls:
                outcome = tools.call(call.call_id, request)
                messages.append(
                    Message(role="tool", content=outcome.content, tool_call_id=request.id, provenance="tool_output")
                )
        rec.outcome(status="ok")
        return rec.run_id


class Recorded:
    """One recorded run, its local store view, and its deterministic bundle."""

    def __init__(self, root: Path, name: str, second_tool: str) -> None:
        self.store_path = root / f"store-{name}"
        self.run_id = _record(self.store_path, second_tool=second_tool)
        store = Store(self.store_path)
        try:
            self.header = store.run(self.run_id)
            self.events = store.events(self.run_id)
            cassette = export_cassette(store, self.run_id, root / f"cassette-{name}")
        finally:
            store.close()
        self.path = build_bundle(cassette, root / f"{name}.locus")
        self.raw = self.path.read_bytes()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.validated = validate_bundle(self.path)


class FakeControlPlane(WorkerClient):
    """Answers the worker protocol without a control plane or object store."""

    def __init__(self, inputs: dict[str, bytes]) -> None:
        super().__init__("http://worker.invalid", WORKER_ID, "token")
        self.inputs = inputs
        self.declarations: list[dict[str, Any]] = []
        self.completions: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []
        self.acked = False

    def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("/ack"):
            self.acked = True
        return 204, b""

    def json(self, method: str, path: str, value: Any = None, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("/artifacts"):
            self.declarations.append(value)
            key = f"workspaces/w/jobs/{JOB_ID}/attempts/1/{value['kind']}"
            return {
                "protocol_version": 1,
                "object_key": key,
                "upload_url": f"memory:{key}",
                "upload_method": "PUT",
                "upload_headers": {},
                "required_digest": value["digest"],
                "required_size": value["size"],
            }
        if "/inputs/" in path:
            artifact = path.rsplit("/", 1)[1]
            raw = self.inputs[artifact]
            return {
                "protocol_version": 1,
                "artifact_id": artifact,
                "object_key": f"workspaces/w/runs/{artifact}/bundle.tar",
                "object_version": "version-1",
                "download_url": f"memory:input:{artifact}",
                "digest": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "media_type": "application/x-tar",
            }
        if path.endswith("/progress"):
            self.progress.append(value)
        if path.endswith("/complete"):
            self.completions.append(value)
        if path.endswith("/fail"):
            self.failures.append(value)
        return None


@pytest.fixture
def objects(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    stored: dict[str, bytes] = {}

    def fetch(url: str) -> bytes:
        return stored[url]

    def store(grant: dict[str, Any], data: bytes) -> str:
        stored[grant["upload_url"]] = data
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(worker, "_fetch_object", fetch)
    monkeypatch.setattr(worker, "_store_object", store)
    return stored


@pytest.fixture
def good(tmp_path: Path) -> Recorded:
    return Recorded(tmp_path, "good", "edit_file")


@pytest.fixture
def bad(tmp_path: Path) -> Recorded:
    return Recorded(tmp_path, "bad", "delete_file")


def claim_for(operation: str, runs: list[Recorded], *, profile: str | None = None) -> dict[str, Any]:
    return {
        "job_id": JOB_ID,
        "attempt_number": 1,
        "attempt_token": "attempt",
        "operation": operation,
        "profile": profile if profile is not None else ("lexical-v1" if operation == "diff" else None),
        "input_artifacts": [
            {
                "artifact_id": RUN_IDS[index],
                "object_key": f"workspaces/w/runs/{RUN_IDS[index]}/bundle.tar",
                "object_version": "version-1",
                "digest": run.digest,
                "size": len(run.raw),
                "media_type": "application/x-tar",
            }
            for index, run in enumerate(runs)
        ],
    }


def deploy(objects: dict[str, bytes], runs: list[Recorded]) -> FakeControlPlane:
    for index, run in enumerate(runs):
        objects[f"memory:input:{RUN_IDS[index]}"] = run.raw
    return FakeControlPlane({RUN_IDS[index]: run.raw for index, run in enumerate(runs)})


def companion(client: FakeControlPlane, objects: dict[str, bytes], output: dict[str, Any]) -> bytes:
    reference = output["companions"][0]
    return objects[f"memory:{reference['object_key']}"]


def result_of(output: dict[str, Any]) -> dict[str, Any]:
    return output["envelope"]["result"]


def test_hosted_spans_match_a_local_export_of_the_same_run(tmp_path: Path, objects, good: Recorded) -> None:
    client = deploy(objects, [good])
    output = worker._otlp(client, claim_for("otlp", [good]), tmp_path)

    raw = companion(client, objects, output)
    assert raw == encode_spans(bundle_header(good.validated), list(good.validated.events))[0]
    document = json.loads(raw)
    hosted = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    local = build_spans(good.header, good.events)["resourceSpans"][0]["scopeSpans"][0]["spans"]

    assert result_of(output)["span_count"] == len(hosted) == len(local)
    # A bundle carries events, not the recording session's own header, so only
    # the root span's descriptive attributes can differ.
    assert hosted[1:] == local[1:]
    assert hosted[0]["spanId"] == local[0]["spanId"]
    assert hosted[0]["traceId"] == local[0]["traceId"]
    assert hosted[0]["status"]["code"] == local[0]["status"]["code"]
    # The session's own start and end are not recorded in a bundle, so the
    # hosted root span covers the events instead of the whole session.
    assert local[0]["startTimeUnixNano"] <= hosted[0]["startTimeUnixNano"]
    assert hosted[0]["endTimeUnixNano"] <= local[0]["endTimeUnixNano"]


def test_hosted_profile_matches_local_token_attribution(tmp_path: Path, objects, good: Recorded) -> None:
    client = deploy(objects, [good])
    output = worker._pprof(client, claim_for("pprof", [good]), tmp_path)

    profile = companion(client, objects, output)
    decoded = read_gzipped_profile(io.BytesIO(profile))
    local_bytes = gzip_profile(build_token_profile(good.header, good.events))
    local = read_gzipped_profile(io.BytesIO(local_bytes))

    assert sample_totals(decoded) == sample_totals(local) == usage_totals(good.events)
    assert result_of(output)["sample_count"] == len(decoded["samples"])
    hosted_shares = attribute_tokens(bundle_header(good.validated), list(good.validated.events))
    local_shares = attribute_tokens(good.header, good.events)
    assert [(s.model_id, s.turn, s.leaf, s.input_tokens, s.output_tokens) for s in hosted_shares] == [
        (s.model_id, s.turn, s.leaf, s.input_tokens, s.output_tokens) for s in local_shares
    ]


def test_hosted_diff_matches_a_local_comparison(tmp_path: Path, objects, good: Recorded, bad: Recorded) -> None:
    client = deploy(objects, [good, bad])
    output = worker._diff(client, claim_for("diff", [good, bad]), tmp_path)

    local = diff_runs(good.events, bad.events, embed=LexicalEmbedder(), embedding_model="lexical-v1")
    result = result_of(output)
    assert result["score"] == local.score
    assert result["divergence"] == local.divergence
    assert result["good_step_count"] == len(local.good_steps)
    assert result["bad_step_count"] == len(local.bad_steps)

    html = companion(client, objects, output).decode("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "http://" not in html and "https://" not in html


@pytest.mark.parametrize("operation", ["otlp", "pprof", "diff"])
def test_analyses_are_deterministic_from_normalized_inputs(
    tmp_path: Path, objects, good: Recorded, bad: Recorded, operation: str
) -> None:
    runs = [good, bad] if operation == "diff" else [good]
    handler = {"otlp": worker._otlp, "pprof": worker._pprof, "diff": worker._diff}[operation]
    client = deploy(objects, runs)
    for name in ("first", "second"):
        (tmp_path / name).mkdir()

    first = handler(client, claim_for(operation, runs), tmp_path / "first")
    second = handler(client, claim_for(operation, runs), tmp_path / "second")

    assert first["companions"][0]["digest"] == second["companions"][0]["digest"]
    assert companion(client, objects, first) == companion(client, objects, second)
    stable = {"analysis_profile", "inputs", "locus_version", "worker_build"}
    for value in (first, second):
        result = result_of(value)
        result.pop("artifact", None)
        result.pop("html", None)
        result["provenance"] = {k: v for k, v in result["provenance"].items() if k in stable}
    assert result_of(first) == result_of(second)


@pytest.mark.parametrize(
    "operation,profile",
    [
        ("diff", "mlx-community/bge-small-en-v1.5-bf16"),
        ("diff", None),
        ("otlp", "mlx-community/bge-small-en-v1.5-bf16"),
        ("pprof", "lexical-v1"),
        ("verify", None),
    ],
)
def test_unsupported_analysis_profiles_are_rejected(operation: str, profile: str | None) -> None:
    with pytest.raises(UnsupportedAnalysis):
        worker._operation({"operation": operation, "profile": profile})


def test_an_unsupported_profile_fails_permanently(objects, good: Recorded) -> None:
    claim = claim_for("diff", [good], profile="mlx-community/bge-small-en-v1.5-bf16")
    claim["input_artifacts"].append(claim["input_artifacts"][0])

    class Claiming(FakeControlPlane):
        def json(self, method: str, path: str, value: Any = None, **kwargs):  # type: ignore[no-untyped-def]
            if path.endswith("/claims"):
                return claim
            return super().json(method, path, value, **kwargs)

    client = Claiming(deploy(objects, [good]).inputs)

    class Source:
        def next(self):
            return worker.Delivery(
                notification={"protocol_version": 1, "job_id": JOB_ID, "job_version": 1, "operation": "diff"},
                acknowledge=lambda: setattr(client, "acked", True),
                extend_visibility=lambda _seconds: None,
            )

    assert run_once(client, Source())
    assert client.acked
    assert client.completions == []
    assert client.failures == [
        {
            "schema_version": 1,
            "code": "unsupported_version",
            "message": "requested analysis is not supported by this worker",
            "retryable": False,
        }
    ]


def test_an_oversized_output_is_refused_before_upload(objects, good: Recorded, monkeypatch: pytest.MonkeyPatch) -> None:
    client = deploy(objects, [good])
    monkeypatch.setattr(worker, "MAX_ARTIFACT_BYTES", 8)
    with pytest.raises(ValueError, match="limit is 8"):
        worker._upload(client, claim_for("otlp", [good]), "otlp_json", b"x" * 9, "application/json")
    assert client.declarations == []
