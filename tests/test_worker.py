from __future__ import annotations

import json
from pathlib import Path

from locus.worker import LeaseLost, WorkerClient, _BundleBlobs, _validate, run_once


class FakeClient(WorkerClient):
    def __init__(self, bundle: bytes) -> None:
        super().__init__("http://worker.invalid", "00000000-0000-4000-8000-000000000001", "token")
        self.bundle = bundle
        self.acked = False

    def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        if path.endswith("/ack"):
            self.acked = True
            return 204, b""
        return 200, self.bundle


def test_bundle_blobs_implement_report_lookup() -> None:
    blobs = _BundleBlobs({"abc": b"value"})
    assert blobs.has("abc")
    assert not blobs.has("missing")
    assert blobs.get("abc") == b"value"


def test_validation_worker_uses_bundle_semantics(tmp_path: Path) -> None:
    bundle = Path("contracttest/fixtures/v1/accepted/bundle-v1.tar").read_bytes()
    client = FakeClient(bundle)
    claim = {
        "job_id": "00000000-0000-4000-8000-000000000002",
        "attempt_number": 1,
        "attempt_token": "attempt",
        "input_artifacts": [
            {
                "artifact_id": "018f7f28-df62-7bc4-9f45-6e6c32a19484",
                "object_key": "workspaces/w/runs/r/bundle.tar",
                "object_version": "version-1",
            }
        ],
    }
    result = _validate(client, claim, tmp_path)
    assert result["envelope"]["result"]["kind"] == "validation"
    assert result["logical_run_digest"] == "c9ebc0bb8168a2a84dcf20f17f885f7a6301ce0f25ee52db9a9e7c0a4abcc00f"


def test_obsolete_notification_is_acknowledged() -> None:
    class Obsolete(FakeClient):
        def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            if path.endswith("/ack"):
                self.acked = True
                return 204, b""
            if path.endswith("/next"):
                return 200, json.dumps({"notification_id": 7, "notification": {"job_id": "00000000-0000-4000-8000-000000000002"}}).encode()
            return super().request(method, path, **kwargs)

        def json(self, method: str, path: str, value, **kwargs):  # type: ignore[no-untyped-def]
            raise LeaseLost("obsolete")

    client = Obsolete(b"")
    assert run_once(client)
    assert client.acked
