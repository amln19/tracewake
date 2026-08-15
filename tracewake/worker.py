from __future__ import annotations

import _thread
import contextvars
import hashlib
import json
import logging
import os
import signal
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .align import LexicalEmbedder, diff_runs
from .bundle import ValidatedBundle, bundle_header, validate_bundle
from .contracts import AlignmentColumn, ArtifactRef, DiffResult as ContractDiffResult, OtlpResult, PprofResult, ResultEnvelope, ResultProvenance, RunProvenance, ValidationResult
from .otel import encode_spans
from .pprof import attribute_tokens, build_token_profile, gzip_profile
from .report import write_report
from .telemetry import Span, Telemetry


LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20
CANCELLATION_SECONDS = 1
# One attempt output. Analyses summarise a bundle instead of copying it, so a
# larger output means a defect rather than a large run.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
HOSTED_PROFILE = "align-v1"

log = logging.getLogger("tracewake.worker")


@dataclass
class _Execution:
    """Telemetry for the attempt running on this thread."""

    telemetry: Telemetry
    operation: str
    parents: list[Span]
    stages: dict[str, float] = field(default_factory=dict)

    def transferred(self) -> float:
        return self.stages.get("download", 0.0) + self.stages.get("upload", 0.0)


_execution: contextvars.ContextVar[_Execution | None] = contextvars.ContextVar("tracewake_worker_execution", default=None)


@contextmanager
def _stage(name: str) -> Iterator[None]:
    """Time one stage of the current attempt and attribute it to that stage."""
    execution = _execution.get()
    if execution is None:
        yield
        return
    started = time.perf_counter()
    with execution.telemetry.span(f"worker.{name}", parent=execution.parents[-1]) as span:
        execution.parents.append(span)
        try:
            yield
        finally:
            execution.parents.pop()
            execution.stages[name] = execution.stages.get(name, 0.0) + (time.perf_counter() - started) * 1000


class LeaseLost(RuntimeError):
    pass


class UnsupportedAnalysis(Exception):
    """A claim names work this worker will not perform under any retry."""


@dataclass
class Delivery:
    """One at-least-once notification and the transport actions it allows."""

    notification: dict[str, Any]
    acknowledge: Callable[[], None]
    extend_visibility: Callable[[int], None]


class ControlPlaneNotifications:
    """Reads notifications straight from the control plane's outbox."""

    def __init__(self, client: WorkerClient) -> None:
        self._client = client

    def next(self) -> Delivery | None:
        status, raw = self._client.request("GET", "/internal/v1/notifications/next")
        if status == 204:
            return None
        delivery = json.loads(raw)
        identifier = delivery["notification_id"]
        return Delivery(
            notification=delivery["notification"],
            acknowledge=lambda: self._client.request("POST", f"/internal/v1/notifications/{identifier}/ack"),
            extend_visibility=lambda _seconds: None,
        )


class QueueNotifications:
    """Consumes hosted job notifications from SQS."""

    def __init__(self, queue_url: str, client: Any = None) -> None:
        if client is None:
            import boto3  # imported lazily so local Tracewake never needs an AWS SDK

            client = boto3.client("sqs")
        self._client = client
        self._queue_url = queue_url

    def next(self) -> Delivery | None:
        response = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=10,
            VisibilityTimeout=LEASE_SECONDS,
        )
        messages = response.get("Messages") or []
        if not messages:
            return None
        message = messages[0]
        handle = message["ReceiptHandle"]
        return Delivery(
            notification=json.loads(message["Body"]),
            acknowledge=lambda: self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=handle),
            extend_visibility=lambda seconds: self._client.change_message_visibility(
                QueueUrl=self._queue_url, ReceiptHandle=handle, VisibilityTimeout=seconds
            ),
        )


class _BundleBlobs:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def has(self, digest: str) -> bool:
        return digest in self._blobs

    def get(self, digest: str) -> bytes:
        return self._blobs[digest]


class WorkerClient:
    def __init__(self, base_url: str, worker_id: str, token: str, ca_pem: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.token = token
        self._ssl_context = ssl.create_default_context(cadata=ca_pem) if ca_pem else None
        # Sending the attempt's trace context makes the control plane's own
        # spans part of the trace that started with the job request.
        self.traceparent: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        attempt_token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request_headers = {"Authorization": f"Bearer {self.token}"}
        if self.traceparent:
            request_headers["traceparent"] = self.traceparent
        request_headers.update(headers or {})
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        if attempt_token is not None:
            request_headers["Tracewake-Attempt-Token"] = attempt_token
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=request_headers, method=method
        )
        try:
            options: dict[str, Any] = {"timeout": 30}
            if self._ssl_context is not None:
                options["context"] = self._ssl_context
            with urllib.request.urlopen(request, **options) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise LeaseLost("attempt lease is no longer current") from exc
            raise RuntimeError(f"worker request failed with HTTP {exc.code}") from exc

    def json(self, method: str, path: str, value: Any = None, *, attempt_token: str | None = None) -> Any:
        body = None if value is None else json.dumps(value, separators=(",", ":")).encode()
        status, raw = self.request(method, path, body=body, attempt_token=attempt_token)
        return None if status == 204 or not raw else json.loads(raw)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fetch_object(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=120) as response:
        return response.read()


def _store_object(grant: dict[str, Any], data: bytes) -> str:
    """Upload through a short-lived grant and return the immutable object version."""
    request = urllib.request.Request(
        grant["upload_url"],
        data=data,
        headers=grant.get("upload_headers") or {},
        method=grant.get("upload_method", "PUT"),
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        version = response.headers.get("x-amz-version-id") or response.headers.get("Tracewake-Object-Version")
    if not version:
        raise RuntimeError("object store did not report an immutable object version")
    return version


def _heartbeat(
    client: WorkerClient,
    job: str,
    attempt: int,
    token: str,
    stop: threading.Event,
    delivery: Delivery | None = None,
    interrupt: Callable[[str], None] | None = None,
) -> None:
    path = f"/internal/v1/jobs/{job}/attempts/{attempt}/heartbeat"
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            client.request("PUT", path, body=_canonical({"protocol_version": 1, "attempt_number": attempt, "observed_lease_expires_at": "1970-01-01T00:00:00Z"}), attempt_token=token)
            # Queue visibility is extended only after the database lease is
            # proven current, so a fenced worker stops holding the message.
            if delivery is not None:
                delivery.extend_visibility(LEASE_SECONDS)
        except Exception as exc:
            # Losing the heartbeat abandons the attempt, so the reason has to
            # reach the operator instead of stopping the thread silently.
            log.warning("job %s attempt %d stopped heartbeating: %s: %s", job, attempt, type(exc).__name__, str(exc)[:200])
            if interrupt is None:
                stop.set()
            else:
                interrupt("lease_lost")
            return


def _watch_cancellation(
    client: WorkerClient,
    job: str,
    attempt: int,
    token: str,
    stop: threading.Event,
    interrupt: Callable[[str], None],
) -> None:
    path = f"/internal/v1/jobs/{job}/attempts/{attempt}/cancellation"
    while not stop.wait(CANCELLATION_SECONDS):
        try:
            response = client.json("GET", path, None, attempt_token=token)
            if response["cancel_requested"]:
                interrupt("cancelled")
                return
        except LeaseLost:
            interrupt("lease_lost")
            return
        except Exception as exc:
            log.warning(
                "job %s attempt %d could not check cancellation: %s",
                job,
                attempt,
                type(exc).__name__,
            )


def _download_input(client: WorkerClient, claim: dict[str, Any], artifact: dict[str, Any], destination: Path) -> ValidatedBundle:
    job = claim["job_id"]
    attempt = claim["attempt_number"]
    path = f"/internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact['artifact_id']}"
    with _stage("download"):
        reference = client.json("GET", path, None, attempt_token=claim["attempt_token"])
        destination.write_bytes(_fetch_object(reference["download_url"]))
        return validate_bundle(destination)


def _validate(client: WorkerClient, claim: dict[str, Any], root: Path) -> dict[str, Any]:
    artifact = claim["input_artifacts"][0]
    validated = _download_input(client, claim, artifact, root / "bundle.tar")
    if validated.bundle_digest != artifact["digest"]:
        raise ValueError("stored bundle bytes do not match the declared upload digest")
    manifest = validated.manifest
    provenance = ResultProvenance(
        inputs=[
            RunProvenance(
                run_id=artifact["artifact_id"],
                logical_run_digest=validated.logical_run_digest,
                bundle_digest=validated.bundle_digest,
                bundle_object_key=artifact["object_key"],
                bundle_object_version=artifact["object_version"],
                event_schema_version=manifest.event_schema_version,
                cassette_format_version=manifest.cassette_format_version,
                bundle_format_version=manifest.bundle_format_version,
            )
        ],
        analysis_profile="bundle-validation-v1",
        tracewake_version=version("tracewake"),
        worker_build=os.environ.get("TRACEWAKE_WORKER_BUILD", "local"),
        produced_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    result = ValidationResult(
        schema_version=1,
        valid=True,
        run_id=artifact["artifact_id"],
        event_count=len(validated.events),
        logical_run_digest=validated.logical_run_digest,
        bundle_digest=validated.bundle_digest,
        provenance=provenance,
    )
    envelope = ResultEnvelope(protocol_version=1, status="succeeded", result=result)
    return {
        "envelope": envelope.model_dump(mode="json"),
        "logical_run_digest": validated.logical_run_digest,
        "event_count": len(validated.events),
        "bundle_format_version": manifest.bundle_format_version,
        "cassette_format_version": manifest.cassette_format_version,
        "event_schema_version": manifest.event_schema_version,
        "bundle_digest": validated.bundle_digest,
        "kind": "validation_json",
        "companions": [],
    }


def _provenance(artifact: dict[str, Any], bundle: ValidatedBundle) -> RunProvenance:
    manifest=bundle.manifest
    return RunProvenance(run_id=artifact["artifact_id"],logical_run_digest=bundle.logical_run_digest,bundle_digest=bundle.bundle_digest,bundle_object_key=artifact["object_key"],bundle_object_version=artifact["object_version"],event_schema_version=manifest.event_schema_version,cassette_format_version=manifest.cassette_format_version,bundle_format_version=manifest.bundle_format_version)


def _upload(client: WorkerClient, claim: dict[str, Any], kind: str, data: bytes, media_type: str) -> dict[str, Any]:
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"{kind} output is {len(data)} bytes; limit is {MAX_ARTIFACT_BYTES}")
    digest = hashlib.sha256(data).hexdigest()
    job = claim["job_id"]
    attempt = claim["attempt_number"]
    with _stage("upload"):
        grant = client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/artifacts",
            {"protocol_version": 1, "attempt_number": attempt, "kind": kind, "media_type": media_type, "digest": digest, "size": len(data)},
            attempt_token=claim["attempt_token"],
        )
        return {
            "artifact_id": str(uuid.uuid4()),
            "kind": kind,
            "media_type": media_type,
            "object_key": grant["object_key"],
            "object_version": _store_object(grant, data),
            "digest": digest,
            "size": len(data),
        }


def _reference(identity: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(artifact_id=identity["artifact_id"],object_key=identity["object_key"],object_version=identity["object_version"],digest=identity["digest"],size=identity["size"],media_type=identity["media_type"])


def _result_provenance(claim: dict[str, Any], bundles: list[ValidatedBundle], profile: str) -> ResultProvenance:
    return ResultProvenance(inputs=[_provenance(artifact,bundle) for artifact,bundle in zip(claim["input_artifacts"],bundles,strict=True)],analysis_profile=profile,tracewake_version=version("tracewake"),worker_build=os.environ.get("TRACEWAKE_WORKER_BUILD","local"),produced_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()))


def _analysis(envelope: ResultEnvelope, kind: str, companion: dict[str, Any]) -> dict[str, Any]:
    """An analysis commits its result and the artifact that result names.

    Only validation reports run identity, so the fields that make a run usable
    stay empty here and the control plane leaves the run untouched.
    """
    return {"envelope":envelope.model_dump(mode="json"),"kind":kind,"companions":[{**companion,"schema_name":None,"schema_version":None}],"logical_run_digest":"","event_count":0,"bundle_digest":"","bundle_format_version":0,"cassette_format_version":0,"event_schema_version":0}


def _diff(client: WorkerClient,claim: dict[str,Any],root: Path) -> dict[str,Any]:
    bundles=[_download_input(client,claim,artifact,root/f"bundle-{index}.tar") for index,artifact in enumerate(claim["input_artifacts"])]
    result=diff_runs(bundles[0].events,bundles[1].events,embed=LexicalEmbedder(),embedding_model=HOSTED_PROFILE)
    html_path=root/"report.html";write_report(html_path,bundle_header(bundles[0]),list(bundles[0].events),bundle_header(bundles[1]),list(bundles[1].events),result,blobs=_BundleBlobs(bundles[0].blobs),blobs_b=_BundleBlobs(bundles[1].blobs))
    companion=_upload(client,claim,"diff_html",html_path.read_bytes(),"text/html; charset=utf-8")
    columns=[AlignmentColumn(good_index=i,bad_index=j,similarity=result.column_similarity(i,j)) for i,j in result.alignment]
    semantic=ContractDiffResult(schema_version=1,profile=HOSTED_PROFILE,score=result.score,divergence=result.divergence,good_step_count=len(result.good_steps),bad_step_count=len(result.bad_steps),alignment=columns,provenance=_result_provenance(claim,bundles,HOSTED_PROFILE),html=_reference(companion))
    return _analysis(ResultEnvelope(protocol_version=1,status="succeeded",result=semantic),"diff_json",companion)


def _otlp(client: WorkerClient,claim: dict[str,Any],root: Path) -> dict[str,Any]:
    bundle=_download_input(client,claim,claim["input_artifacts"][0],root/"bundle.tar")
    document,spans=encode_spans(bundle_header(bundle),list(bundle.events))
    companion=_upload(client,claim,"otlp_json",document,"application/json")
    semantic=OtlpResult(schema_version=1,span_count=spans,provenance=_result_provenance(claim,[bundle],"otlp-spans-v1"),artifact=_reference(companion))
    return _analysis(ResultEnvelope(protocol_version=1,status="succeeded",result=semantic),"otlp_result_json",companion)


def _pprof(client: WorkerClient,claim: dict[str,Any],root: Path) -> dict[str,Any]:
    bundle=_download_input(client,claim,claim["input_artifacts"][0],root/"bundle.tar")
    header=bundle_header(bundle);events=list(bundle.events)
    profile=gzip_profile(build_token_profile(header,events))
    companion=_upload(client,claim,"pprof",profile,"application/octet-stream")
    semantic=PprofResult(schema_version=1,sample_count=len(attribute_tokens(header,events)),provenance=_result_provenance(claim,[bundle],"pprof-tokens-v1"),artifact=_reference(companion))
    return _analysis(ResultEnvelope(protocol_version=1,status="succeeded",result=semantic),"pprof_result_json",companion)


def _operation(claim: dict[str, Any]) -> Callable[[WorkerClient, dict[str, Any], Path], dict[str, Any]]:
    """Refuse work this worker cannot perform exactly as the job asked.

    A profile the hosted path does not implement — an MLX embedder, say — is a
    permanent mismatch, not a dependency that a retry could satisfy.
    """
    operation = claim["operation"]
    expected = HOSTED_PROFILE if operation == "diff" else None
    if claim.get("profile") != expected:
        raise UnsupportedAnalysis(f"{operation} does not support the requested analysis profile")
    handlers = {"validate": _validate, "diff": _diff, "otlp": _otlp, "pprof": _pprof}
    if operation not in handlers:
        raise UnsupportedAnalysis(f"unsupported worker operation {operation}")
    return handlers[operation]


def run_once(client: WorkerClient, notifications: Any = None, telemetry: Telemetry | None = None) -> bool:
    source = notifications if notifications is not None else ControlPlaneNotifications(client)
    delivery = source.next()
    if delivery is None:
        return False
    recorder = telemetry if telemetry is not None else Telemetry.from_environment()
    with recorder.span("worker.execute", kind="consumer", parent=delivery.notification.get("traceparent")) as span:
        client.traceparent = span.traceparent()
        try:
            return _attempt(client, delivery, recorder, span)
        finally:
            client.traceparent = None


def _report(execution: _Execution, outcome: str) -> None:
    execution.telemetry.job_finished(execution.operation, outcome)
    for stage, milliseconds in execution.stages.items():
        execution.telemetry.stage_finished(execution.operation, stage, milliseconds)


def _attempt(client: WorkerClient, delivery: Delivery, recorder: Telemetry, span: Span) -> bool:
    try:
        claim = client.json(
            "POST",
            "/internal/v1/claims",
            {"protocol_version": 1, "notification": delivery.notification, "worker_id": client.worker_id},
        )
    except LeaseLost:
        delivery.acknowledge()
        return True
    job, attempt, attempt_token = claim["job_id"], claim["attempt_number"], claim["attempt_token"]
    span.set(job_id=job, attempt=attempt, operation=claim["operation"])
    execution = _Execution(recorder, claim["operation"], [span])
    scope = _execution.set(execution)
    stop = threading.Event()
    stop_reason: list[str] = []
    interrupt_lock = threading.Lock()

    def interrupt(reason: str) -> None:
        with interrupt_lock:
            if stop.is_set():
                return
            stop_reason.append(reason)
            stop.set()
        _thread.interrupt_main()

    heartbeat = threading.Thread(
        target=_heartbeat,
        args=(client, job, attempt, attempt_token, stop, delivery, interrupt),
        daemon=True,
    )
    cancellation = threading.Thread(
        target=_watch_cancellation,
        args=(client, job, attempt, attempt_token, stop, interrupt),
        daemon=True,
    )
    try:
        heartbeat.start()
        cancellation.start()
        progress_path=f"/internal/v1/jobs/{job}/attempts/{attempt}/progress"
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":1,"stage":"downloading","message":"downloading immutable inputs"},attempt_token=attempt_token)
        handler = _operation(claim)
        transferred = execution.transferred()
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="tracewake-worker-") as temporary:
            stage="validating" if claim["operation"]=="validate" else "analyzing"
            client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":2,"stage":stage,"message":f"{stage} recorded runs"},attempt_token=attempt_token)
            output = handler(client, claim, Path(temporary))
        # Analysis time is the handler's own work: the transfers it performed
        # are already attributed to their own stages.
        elapsed = (time.perf_counter() - started) * 1000
        execution.stages["analyze"] = max(elapsed - (execution.transferred() - transferred), 0.0)
        if stop.is_set():
            raise LeaseLost("attempt lease was lost before its result could commit")
        artifact_bytes = _canonical(output["envelope"])
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":3,"stage":"uploading","message":"uploading immutable results"},attempt_token=attempt_token)
        identity = _upload(client,claim,output["kind"],artifact_bytes,"application/json")
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":4,"stage":"committing","message":"committing authoritative result"},attempt_token=attempt_token)
        with _stage("commit"):
            client.json(
                "POST",
                f"/internal/v1/jobs/{job}/attempts/{attempt}/complete",
                {
                    **identity,
                    "schema_name": "result-envelope",
                    "schema_version": 1,
                    "logical_run_digest": output["logical_run_digest"],
                    "event_count": output["event_count"],
                    "bundle_digest": output["bundle_digest"],
                    "bundle_format_version": output["bundle_format_version"],
                    "cassette_format_version": output["cassette_format_version"],
                    "event_schema_version": output["event_schema_version"],
                    "companions": output["companions"],
                },
                attempt_token=attempt_token,
            )
        delivery.acknowledge()
        _report(execution, "succeeded")
        return True
    except KeyboardInterrupt:
        if stop_reason:
            reason = stop_reason[0]
            log.warning("job %s attempt %d interrupted: %s", job, attempt, reason)
            _report(execution, "fenced")
            if reason == "cancelled":
                delivery.acknowledge()
            return True
        _report(execution, "failed")
        try:
            client.json(
                "POST",
                f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
                {
                    "schema_version": 1,
                    "code": "internal",
                    "message": "worker is shutting down",
                    "retryable": True,
                },
                attempt_token=attempt_token,
            )
            delivery.acknowledge()
        except Exception as exc:
            log.warning(
                "job %s attempt %d could not report shutdown: %s",
                job,
                attempt,
                type(exc).__name__,
            )
        raise
    except LeaseLost:
        # A superseded attempt leaves the notification for redelivery rather
        # than deleting work the current attempt may still need.
        log.warning("job %s attempt %d abandoned: lease is no longer current", job, attempt)
        _report(execution, "fenced")
        return True
    except UnsupportedAnalysis:
        log.warning("job %s attempt %d rejected: unsupported analysis", job, attempt)
        _report(execution, "refused")
        client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
            {"schema_version": 1, "code": "unsupported_version", "message": "requested analysis is not supported by this worker", "retryable": False},
            attempt_token=attempt_token,
        )
        delivery.acknowledge()
        return True
    except ValueError:
        _report(execution, "failed")
        client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
            {"schema_version": 1, "code": "invalid_bundle" if claim["operation"] == "validate" else "invalid_result", "message": "worker operation failed", "retryable": False},
            attempt_token=attempt_token,
        )
        delivery.acknowledge()
        return True
    except Exception:
        _report(execution, "failed")
        client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
            {"schema_version": 1, "code": "internal", "message": "worker operation failed", "retryable": True},
            attempt_token=attempt_token,
        )
        delivery.acknowledge()
        return True
    finally:
        _execution.reset(scope)
        stop.set()
        heartbeat.join(timeout=1)
        cancellation.join(timeout=1)


def _resolve_identity(client: WorkerClient, attempts: int = 30) -> str:
    """A worker may start before its control plane is reachable."""
    for remaining in range(attempts, 0, -1):
        try:
            return str(client.json("GET", "/internal/v1/identity")["worker_id"])
        except (OSError, RuntimeError):
            if remaining == 1:
                raise
            time.sleep(4)
    raise RuntimeError("worker identity could not be resolved")


def _supervise(client: WorkerClient, notifications: Any, telemetry: Telemetry) -> None:
    while True:
        try:
            if not run_once(client, notifications, telemetry):
                time.sleep(1)
        except Exception as exc:
            log.warning("worker loop recovered from %s", type(exc).__name__)
            time.sleep(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    credentials: dict[str, str] = {}
    if path := os.environ.get("TRACEWAKE_WORKER_CREDENTIALS_FILE"):
        while not Path(path).exists():
            time.sleep(0.2)
        credentials = json.loads(Path(path).read_text(encoding="utf-8"))
    client = WorkerClient(
        os.environ.get("TRACEWAKE_WORKER_URL", "http://127.0.0.1:8081"),
        os.environ.get("TRACEWAKE_WORKER_ID", credentials.get("worker_id", "")),
        os.environ.get("TRACEWAKE_WORKER_TOKEN", credentials.get("worker_token", "")),
        os.environ.get("TRACEWAKE_WORKER_CA_PEM", ""),
    )
    if not client.worker_id:
        client.worker_id = _resolve_identity(client)
    queue_url = os.environ.get("TRACEWAKE_JOB_QUEUE_URL", "")
    notifications: Any = QueueNotifications(queue_url) if queue_url else ControlPlaneNotifications(client)
    telemetry = Telemetry.from_environment()
    previous_sigterm = signal.signal(signal.SIGTERM, lambda _signum, _frame: _thread.interrupt_main())
    try:
        _supervise(client, notifications, telemetry)
    except KeyboardInterrupt:
        return
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
