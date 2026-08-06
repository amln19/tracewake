from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any

import typer

from .bundle import validate_bundle

app = typer.Typer(no_args_is_help=True, help="Use a local or hosted Locus control plane.")
URL = Annotated[str, typer.Option("--url", envvar="LOCUS_REMOTE_URL")]
Token = Annotated[str, typer.Option("--token", envvar="LOCUS_TOKEN", hide_input=True)]


def _request(method: str, url: str, token: str, path: str, value: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if value is not None:
        body = json.dumps(value, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw)["error"]["message"]
        except (KeyError, TypeError, ValueError):
            detail = f"HTTP {exc.code}"
        raise typer.BadParameter(f"remote request failed: {detail}") from exc
    return None if not raw else json.loads(raw)


@app.command()
def upload(bundle: Path, url: URL = "http://127.0.0.1:8080", token: Token = "") -> None:
    """Upload and queue mandatory validation for a deterministic bundle."""
    if not token:
        raise typer.BadParameter("pass --token or set LOCUS_TOKEN")
    validated = validate_bundle(bundle)
    raw = bundle.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    grant = _request("POST", url, token, "/v1/runs/uploads", {"bundle_format_version": 1, "bundle_digest": digest, "bundle_size": len(raw)})
    version = _put_object(grant["upload_url"], raw)
    _request(
        "POST",
        url,
        token,
        f"/v1/runs/uploads/{grant['upload_id']}/complete",
        {"object_version": version, "digest": digest, "size": len(raw)},
    )
    typer.echo(f"uploaded run {grant['run_id']} ({validated.logical_run_digest[:12]})")


def _put_object(upload_url: str, data: bytes) -> str:
    """Upload bundle bytes through the short-lived grant, never through the API."""
    request = urllib.request.Request(upload_url, data=data, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            version = response.headers.get("x-amz-version-id") or response.headers.get("Locus-Object-Version")
    except urllib.error.HTTPError as exc:
        raise typer.BadParameter(f"bundle upload failed with HTTP {exc.code}") from exc
    if not version:
        raise typer.BadParameter("object store did not report an immutable object version")
    return version


@app.command("runs")
def runs_(url: URL = "http://127.0.0.1:8080", token: Token = "") -> None:
    """List remote runs."""
    result = _request("GET", url, token, "/v1/runs")
    for run in result["runs"]:
        typer.echo(f"{run['run_id']}  {run['state']:<10}  {run['bundle_digest'][:12]}")


@app.command()
def analyze(
    operation: Annotated[str, typer.Argument(help="diff, otlp, or pprof")],
    run_ids: Annotated[list[str], typer.Argument()],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    url: URL = "http://127.0.0.1:8080",
    token: Token = "",
) -> None:
    """Queue an analysis of ready remote runs."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Idempotency-Key": idempotency_key}
    value = {"operation": operation, "run_ids": run_ids, "profile": "lexical-v1" if operation == "diff" else None}
    request = urllib.request.Request(url.rstrip("/") + "/v1/jobs", data=json.dumps(value).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise typer.BadParameter(f"remote request failed with HTTP {exc.code}") from exc
    typer.echo(f"queued job {result['job_id']}")


@app.command()
def job(job_id: str, url: URL = "http://127.0.0.1:8080", token: Token = "") -> None:
    """Inspect authoritative job state."""
    typer.echo(json.dumps(_request("GET", url, token, f"/v1/jobs/{job_id}"), indent=2))


@app.command()
def cancel(job_id: str, url: URL = "http://127.0.0.1:8080", token: Token = "") -> None:
    """Request cancellation of a remote job."""
    result = _request("POST", url, token, f"/v1/jobs/{job_id}/cancel", {})
    typer.echo(f"job {result['job_id']} is {result['state']}")
