from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .align import LexicalEmbedder, diff_runs
from .bundle import ValidatedBundle, validate_bundle
from .contracts import AlignmentColumn, ArtifactRef, DiffResult as ContractDiffResult, ResultEnvelope, ResultProvenance, RunProvenance, ValidationResult
from .events import RunHeader
from .report import write_report


LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20


class LeaseLost(RuntimeError):
    pass


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
            import boto3  # imported lazily so local Locus never needs an AWS SDK

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
    def __init__(self, base_url: str, worker_id: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.token = token

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
        request_headers.update(headers or {})
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        if attempt_token is not None:
            request_headers["Locus-Attempt-Token"] = attempt_token
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
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
        version = response.headers.get("x-amz-version-id") or response.headers.get("Locus-Object-Version")
    if not version:
        raise RuntimeError("object store did not report an immutable object version")
    return version


def _heartbeat(client: WorkerClient, job: str, attempt: int, token: str, stop: threading.Event, delivery: Delivery | None = None) -> None:
    path = f"/internal/v1/jobs/{job}/attempts/{attempt}/heartbeat"
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            client.request("PUT", path, body=_canonical({"protocol_version": 1, "attempt_number": attempt, "observed_lease_expires_at": "1970-01-01T00:00:00Z"}), attempt_token=token)
            # Queue visibility is extended only after the database lease is
            # proven current, so a fenced worker stops holding the message.
            if delivery is not None:
                delivery.extend_visibility(LEASE_SECONDS)
        except (LeaseLost, OSError, RuntimeError):
            stop.set()
            return


def _download_input(client: WorkerClient, claim: dict[str, Any], artifact: dict[str, Any], destination: Path) -> ValidatedBundle:
    job = claim["job_id"]
    attempt = claim["attempt_number"]
    path = f"/internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact['artifact_id']}"
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
        locus_version=version("locus"),
        worker_build=os.environ.get("LOCUS_WORKER_BUILD", "local"),
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
    digest = hashlib.sha256(data).hexdigest()
    job = claim["job_id"]
    attempt = claim["attempt_number"]
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


def _header(bundle: ValidatedBundle) -> RunHeader:
    times=[item.event.meta.recorded_at for item in bundle.events]
    start=min(times,default=0.0);finish=max(times,default=start)
    return RunHeader(run_id=bundle.manifest.run_id,name=bundle.manifest.run_id,started_at=start,finished_at=finish,status="ok")


def _diff(client: WorkerClient,claim: dict[str,Any],root: Path) -> dict[str,Any]:
    bundles=[_download_input(client,claim,artifact,root/f"bundle-{index}.tar") for index,artifact in enumerate(claim["input_artifacts"])]
    result=diff_runs(bundles[0].events,bundles[1].events,embed=LexicalEmbedder(),embedding_model="lexical-v1")
    html_path=root/"report.html";write_report(html_path,_header(bundles[0]),list(bundles[0].events),_header(bundles[1]),list(bundles[1].events),result,blobs=_BundleBlobs(bundles[0].blobs),blobs_b=_BundleBlobs(bundles[1].blobs))
    html_identity=_upload(client,claim,"diff_html",html_path.read_bytes(),"text/html; charset=utf-8")
    html_ref=ArtifactRef(artifact_id=html_identity["artifact_id"],object_key=html_identity["object_key"],object_version=html_identity["object_version"],digest=html_identity["digest"],size=html_identity["size"],media_type=html_identity["media_type"])
    provenance=ResultProvenance(inputs=[_provenance(artifact,bundle) for artifact,bundle in zip(claim["input_artifacts"],bundles,strict=True)],analysis_profile="lexical-v1",locus_version=version("locus"),worker_build=os.environ.get("LOCUS_WORKER_BUILD","local"),produced_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()))
    columns=[AlignmentColumn(good_index=i,bad_index=j,similarity=result.column_similarity(i,j)) for i,j in result.alignment]
    semantic=ContractDiffResult(schema_version=1,profile="lexical-v1",score=result.score,divergence=result.divergence,good_step_count=len(result.good_steps),bad_step_count=len(result.bad_steps),alignment=columns,provenance=provenance,html=html_ref)
    envelope=ResultEnvelope(protocol_version=1,status="succeeded",result=semantic)
    companion={**html_identity,"schema_name":None,"schema_version":None}
    return {"envelope":envelope.model_dump(mode="json"),"kind":"diff_json","companions":[companion],"logical_run_digest":"","event_count":0,"bundle_digest":"","bundle_format_version":0,"cassette_format_version":0,"event_schema_version":0}


def run_once(client: WorkerClient, notifications: Any = None) -> bool:
    source = notifications if notifications is not None else ControlPlaneNotifications(client)
    delivery = source.next()
    if delivery is None:
        return False
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
    stop = threading.Event()
    thread = threading.Thread(target=_heartbeat, args=(client, job, attempt, attempt_token, stop, delivery), daemon=True)
    thread.start()
    try:
        progress_path=f"/internal/v1/jobs/{job}/attempts/{attempt}/progress"
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":1,"stage":"downloading","message":"downloading immutable inputs"},attempt_token=attempt_token)
        with tempfile.TemporaryDirectory(prefix="locus-worker-") as temporary:
            stage="validating" if claim["operation"]=="validate" else "analyzing"
            client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":2,"stage":stage,"message":f"{stage} recorded runs"},attempt_token=attempt_token)
            if claim["operation"] == "validate":
                output = _validate(client, claim, Path(temporary))
            elif claim["operation"] == "diff":
                output = _diff(client,claim,Path(temporary))
            else:
                raise ValueError(f"unsupported local worker operation {claim['operation']}")
        if stop.is_set():
            raise LeaseLost("attempt lease was lost during validation")
        artifact_bytes = _canonical(output["envelope"])
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":3,"stage":"uploading","message":"uploading immutable results"},attempt_token=attempt_token)
        identity = _upload(client,claim,output["kind"],artifact_bytes,"application/json")
        client.json("PUT",progress_path,{"protocol_version":1,"attempt_number":attempt,"sequence":4,"stage":"committing","message":"committing authoritative result"},attempt_token=attempt_token)
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
        return True
    except LeaseLost:
        # A superseded attempt leaves the notification for redelivery rather
        # than deleting work the current attempt may still need.
        return True
    except ValueError:
        client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
            {"schema_version": 1, "code": "invalid_bundle" if claim["operation"] == "validate" else "invalid_result", "message": "worker operation failed", "retryable": False},
            attempt_token=attempt_token,
        )
        delivery.acknowledge()
        return True
    except Exception:
        client.json(
            "POST",
            f"/internal/v1/jobs/{job}/attempts/{attempt}/fail",
            {"schema_version": 1, "code": "internal", "message": "worker operation failed", "retryable": True},
            attempt_token=attempt_token,
        )
        delivery.acknowledge()
        return True
    finally:
        stop.set()
        thread.join(timeout=1)


def main() -> None:
    credentials: dict[str, str] = {}
    if path := os.environ.get("LOCUS_WORKER_CREDENTIALS_FILE"):
        while not Path(path).exists():
            time.sleep(0.2)
        credentials = json.loads(Path(path).read_text(encoding="utf-8"))
    client = WorkerClient(
        os.environ.get("LOCUS_WORKER_URL", "http://127.0.0.1:8081"),
        os.environ.get("LOCUS_WORKER_ID", credentials.get("worker_id", "")),
        os.environ.get("LOCUS_WORKER_TOKEN", credentials.get("worker_token", "")),
    )
    queue_url = os.environ.get("LOCUS_JOB_QUEUE_URL", "")
    notifications: Any = QueueNotifications(queue_url) if queue_url else ControlPlaneNotifications(client)
    try:
        while True:
            if not run_once(client, notifications):
                time.sleep(1)
    except KeyboardInterrupt:
        return
