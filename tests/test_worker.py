from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from locus import worker
from locus.worker import LeaseLost, WorkerClient, _BundleBlobs, _validate, run_once

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
