from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tracewake import worker
from tracewake.worker import LeaseLost, WorkerClient, _BundleBlobs, _validate, run_once

BUNDLE = Path("contracttest/fixtures/v1/accepted/bundle-v1.tar")


class FakeClient(WorkerClient):
    """Answers the worker protocol without a control plane or object store."""

    def __init__(self, bundle: bytes) -> None:
        super().__init__("http://worker.invalid", "00000000-0000-4000-8000-000000000001", "token")
        self.bundle = bundle
        self.acked = False
        self.objects: dict[str, bytes] = {}

    def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("/ack"):
            self.acked = True
            return 204, b""
        return 200, b""

    def json(self, method: str, path: str, value: Any = None, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("/artifacts"):
            key = f"workspaces/w/jobs/j/attempts/1/{value['kind']}"
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
            return {
                "protocol_version": 1,
                "object_key": "workspaces/w/runs/r/bundle.tar",
                "object_version": "version-1",
                "download_url": "memory:input",
                "digest": hashlib.sha256(self.bundle).hexdigest(),
                "size": len(self.bundle),
                "media_type": "application/x-tar",
            }
        return None


@pytest.fixture
def memory_objects(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    stored: dict[str, bytes] = {}

    def fetch(url: str) -> bytes:
        return stored[url]

    def store(grant: dict[str, Any], data: bytes) -> str:
        stored[grant["upload_url"]] = data
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(worker, "_fetch_object", fetch)
    monkeypatch.setattr(worker, "_store_object", store)
    return stored


def claim_for(operation: str) -> dict[str, Any]:
    return {
        "job_id": "00000000-0000-4000-8000-000000000002",
        "attempt_number": 1,
        "attempt_token": "attempt",
        "operation": operation,
        "input_artifacts": [
            {
                "artifact_id": "018f7f28-df62-7bc4-9f45-6e6c32a19484",
                "object_key": "workspaces/w/runs/r/bundle.tar",
                "object_version": "version-1",
                "digest": hashlib.sha256(BUNDLE.read_bytes()).hexdigest(),
                "size": BUNDLE.stat().st_size,
                "media_type": "application/x-tar",
            }
        ],
    }


def test_bundle_blobs_implement_report_lookup() -> None:
    blobs = _BundleBlobs({"abc": b"value"})
    assert blobs.has("abc")
    assert not blobs.has("missing")
    assert blobs.get("abc") == b"value"


def test_validation_worker_uses_bundle_semantics(tmp_path: Path, memory_objects: dict[str, bytes]) -> None:
    bundle = BUNDLE.read_bytes()
    memory_objects["memory:input"] = bundle
    client = FakeClient(bundle)
    result = _validate(client, claim_for("validate"), tmp_path)
    assert result["envelope"]["result"]["kind"] == "validation"
    assert result["logical_run_digest"] == "c9ebc0bb8168a2a84dcf20f17f885f7a6301ce0f25ee52db9a9e7c0a4abcc00f"
    assert result["bundle_digest"] == hashlib.sha256(bundle).hexdigest()


def test_validation_rejects_bytes_that_do_not_match_the_declaration(tmp_path: Path, memory_objects: dict[str, bytes]) -> None:
    bundle = BUNDLE.read_bytes()
    memory_objects["memory:input"] = bundle
    client = FakeClient(bundle)
    claim = claim_for("validate")
    claim["input_artifacts"][0]["digest"] = "0" * 64
    with pytest.raises(ValueError):
        _validate(client, claim, tmp_path)


def test_uploaded_result_reports_the_committed_object_identity(memory_objects: dict[str, bytes]) -> None:
    client = FakeClient(b"")
    identity = worker._upload(client, claim_for("validate"), "validation_json", b"{}", "application/json")
    assert identity["object_key"] == "workspaces/w/jobs/j/attempts/1/validation_json"
    assert identity["object_version"] == hashlib.sha256(b"{}").hexdigest()
    assert identity["digest"] == identity["object_version"]
    assert memory_objects["memory:workspaces/w/jobs/j/attempts/1/validation_json"] == b"{}"


class FakeQueue:
    """Records the SQS calls a worker is allowed to make."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.deleted: list[str] = []
        self.visibility: list[int] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["VisibilityTimeout"] == worker.LEASE_SECONDS
        if self.body is None:
            return {}
        return {"Messages": [{"ReceiptHandle": "receipt-1", "Body": self.body}]}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int) -> None:
        self.visibility.append(VisibilityTimeout)


def test_queue_delivery_exposes_notification_and_acknowledgement() -> None:
    notification = {"protocol_version": 1, "job_id": "j", "job_version": 1, "operation": "diff"}
    queue = FakeQueue(json.dumps(notification))
    source = worker.QueueNotifications("https://sqs.invalid/queue", client=queue)
    delivery = source.next()
    assert delivery is not None
    assert delivery.notification == notification
    delivery.acknowledge()
    assert queue.deleted == ["receipt-1"]


def test_empty_queue_yields_no_delivery() -> None:
    source = worker.QueueNotifications("https://sqs.invalid/queue", client=FakeQueue(None))
    assert source.next() is None


def test_visibility_is_extended_only_while_the_lease_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    queue = FakeQueue(json.dumps({"job_id": "j"}))
    delivery = worker.QueueNotifications("https://sqs.invalid/queue", client=queue).next()
    assert delivery is not None

    class Live(FakeClient):
        def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            return 204, b""

    stop = threading.Event()
    thread = threading.Thread(target=worker._heartbeat, args=(Live(b""), "j", 1, "token", stop, delivery))
    thread.start()
    while not queue.visibility:
        pass
    stop.set()
    thread.join(timeout=2)
    assert queue.visibility[0] == worker.LEASE_SECONDS

    class Fenced(FakeClient):
        def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            raise LeaseLost("fenced")

    queue.visibility.clear()
    stop = threading.Event()
    worker._heartbeat(Fenced(b""), "j", 1, "token", stop, delivery)
    assert stop.is_set()
    assert queue.visibility == []
    assert queue.deleted == []


def test_obsolete_notification_is_acknowledged() -> None:
    class Obsolete(FakeClient):
        def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            if path.endswith("/ack"):
                self.acked = True
                return 204, b""
            if path.endswith("/next"):
                return 200, json.dumps({"notification_id": 7, "notification": {"job_id": "00000000-0000-4000-8000-000000000002"}}).encode()
            return super().request(method, path, **kwargs)

        def json(self, method: str, path: str, value: Any = None, **kwargs):  # type: ignore[no-untyped-def]
            raise LeaseLost("obsolete")

    client = Obsolete(b"")
    assert run_once(client)
    assert client.acked


class Validating(FakeClient):
    """Answers a complete validation attempt from memory."""

    traces: list[str | None] = []

    def json(self, method: str, path: str, value: Any = None, **kwargs):  # type: ignore[no-untyped-def]
        self.traces.append(self.traceparent)
        if path == "/internal/v1/claims":
            return claim_for("validate")
        if path.endswith("/complete"):
            self.completed = value
            return {"protocol_version": 1, "status": "succeeded"}
        return super().json(method, path, value, **kwargs)


def run_traced(bundle: bytes, traceparent: str | None) -> tuple[Validating, list[dict[str, Any]]]:
    import io

    from tracewake.telemetry import Telemetry

    notification: dict[str, Any] = {"protocol_version": 1, "job_id": "j", "job_version": 1, "operation": "validate"}
    if traceparent is not None:
        notification["traceparent"] = traceparent
    stream = io.StringIO()
    client = Validating(bundle)
    client.traces = []
    delivery = worker.Delivery(notification=notification, acknowledge=lambda: None, extend_visibility=lambda _s: None)
    source = type("Source", (), {"next": lambda _self: delivery})()
    assert run_once(client, source, Telemetry(stream=stream, environment="test"))
    return client, [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_an_attempt_traces_its_stages_within_the_job_trace(memory_objects: dict[str, bytes]) -> None:
    bundle = BUNDLE.read_bytes()
    memory_objects["memory:input"] = bundle
    trace = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    client, emitted = run_traced(bundle, trace)
    assert client.completed is not None
    spans = [record for record in emitted if record["telemetry"] == "span"]
    assert {span["name"] for span in spans} == {
        "worker.execute",
        "worker.download",
        "worker.upload",
        "worker.commit",
    }
    assert {span["trace_id"] for span in spans} == {"4bf92f3577b34da6a3ce929d0e0e4736"}
    execute = next(span for span in spans if span["name"] == "worker.execute")
    assert execute["parent_span_id"] == "00f067aa0ba902b7"
    assert execute["attributes"]["operation"] == "validate"
    for span in spans:
        if span["name"] != "worker.execute":
            assert span["parent_span_id"] == execute["span_id"]
    # Requests made during the attempt carry it, so the control plane's own
    # spans belong to the same trace.
    assert set(client.traces) == {f"00-{execute['trace_id']}-{execute['span_id']}-01"}


def test_an_attempt_reports_bounded_stage_metrics(memory_objects: dict[str, bytes]) -> None:
    bundle = BUNDLE.read_bytes()
    memory_objects["memory:input"] = bundle
    _, emitted = run_traced(bundle, None)
    metrics = {}
    for record in emitted:
        if record["telemetry"] != "metric":
            continue
        for directive in record["_aws"]["CloudWatchMetrics"]:
            for definition in directive["Metrics"]:
                metrics.setdefault(definition["Name"], []).append(record)
    assert metrics["WorkerJobs"][0]["Outcome"] == "succeeded"
    assert metrics["WorkerJobs"][0]["Operation"] == "validate"
    stages = {record["Stage"] for record in metrics["WorkerStageMillis"]}
    assert stages == {"download", "analyze", "upload", "commit"}
    assert all(record["WorkerStageMillis"] >= 0 for record in metrics["WorkerStageMillis"])


def test_an_untraced_notification_still_produces_one_trace(memory_objects: dict[str, bytes]) -> None:
    bundle = BUNDLE.read_bytes()
    memory_objects["memory:input"] = bundle
    _, emitted = run_traced(bundle, None)
    traces = {record["trace_id"] for record in emitted if record["telemetry"] == "span"}
    assert len(traces) == 1


def test_requests_carry_the_attempt_trace_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    seen: dict[str, str] = {}

    class Response:
        status = 204
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b""

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def urlopen(request: Any, timeout: int = 0) -> Response:
        seen.update(request.headers)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = WorkerClient("http://worker.invalid", "worker", "token")
    client.traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    client.request("GET", "/internal/v1/identity")
    assert seen["Traceparent"] == client.traceparent
