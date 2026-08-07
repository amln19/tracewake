"""Minimal clients for the public API and the worker protocol."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class Client:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, value: Any = None, headers: dict[str, str] | None = None) -> Any:
        request_headers = {"Authorization": f"Bearer {self.token}"}
        request_headers.update(headers or {})
        body = None
        if value is not None:
            body = json.dumps(value, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        return None if not raw else json.loads(raw)

    def upload(self, bundle: bytes) -> str:
        digest = hashlib.sha256(bundle).hexdigest()
        grant = self.request(
            "POST", "/v1/runs/uploads",
            {"bundle_format_version": 1, "bundle_digest": digest, "bundle_size": len(bundle)},
        )
        version = put_object(grant["upload_url"], grant.get("upload_headers") or {}, bundle)
        self.request(
            "POST", f"/v1/runs/uploads/{grant['upload_id']}/complete",
            {"object_version": version, "digest": digest, "size": len(bundle)},
        )
        return str(grant["run_id"])

    def run(self, run_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"/v1/runs/{run_id}"))

    def analyze(self, operation: str, run_ids: list[str], key: str, profile: str | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": operation, "run_ids": run_ids}
        if profile is not None:
            request["profile"] = profile
        return dict(self.request("POST", "/v1/jobs", request, {"Idempotency-Key": key}))

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"/v1/jobs/{job_id}"))

    def cancel(self, job_id: str) -> Any:
        return self.request("POST", f"/v1/jobs/{job_id}/cancel")

    def audit(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/v1/audit")["records"])

    def download(self, artifact_id: str) -> bytes:
        grant = self.request("GET", f"/v1/artifacts/{artifact_id}/download")
        with urllib.request.urlopen(grant["download_url"], timeout=120) as response:
            return bytes(response.read())


def put_object(url: str, headers: dict[str, str], data: bytes) -> str:
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=300) as response:
        version = response.headers.get("x-amz-version-id") or response.headers.get("Locus-Object-Version")
    if not version:
        raise RuntimeError("object store did not report an immutable object version")
    return str(version)
