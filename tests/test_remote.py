"""Remote commands verify what a hosted deployment hands back."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracewake.remote import app

RESULT = b'{"protocol_version":1,"status":"succeeded"}\n'


def _serve(payload: bytes) -> tuple[HTTPServer, str]:
    """A control plane that hands out a download URL for `payload`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.endswith("/download"):
                body = json.dumps(
                    {
                        "artifact_id": "a",
                        "download_url": f"http://127.0.0.1:{self.server.server_port}/objects/result",
                        "digest": hashlib.sha256(RESULT).hexdigest(),
                        "size": len(RESULT),
                        "media_type": "application/json",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            else:
                body = payload
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_download_writes_an_artifact_whose_bytes_match(tmp_path: Path, runner: CliRunner) -> None:
    server, url = _serve(RESULT)
    try:
        out = tmp_path / "result.json"
        done = runner.invoke(app, ["download", "artifact-1", "-o", str(out), "--url", url, "--token", "t"])
    finally:
        server.shutdown()
    assert done.exit_code == 0, done.output
    assert out.read_bytes() == RESULT


def test_download_refuses_bytes_that_contradict_the_recorded_identity(tmp_path: Path, runner: CliRunner) -> None:
    server, url = _serve(RESULT + b"tampered")
    try:
        out = tmp_path / "result.json"
        done = runner.invoke(app, ["download", "artifact-1", "-o", str(out), "--url", url, "--token", "t"])
    finally:
        server.shutdown()
    assert done.exit_code != 0
    assert not out.exists()


def test_delete_sends_one_deletion_request(runner: CliRunner) -> None:
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_DELETE(self) -> None:  # noqa: N802
            seen.append((self.command, self.path))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = runner.invoke(
            app,
            ["delete", "018f7f28-df62-7bc4-9f45-6e6c32a19484", "--url", f"http://127.0.0.1:{server.server_port}", "--token", "t"],
        )
    finally:
        server.shutdown()
    assert result.exit_code == 0, result.output
    assert seen == [("DELETE", "/v1/runs/018f7f28-df62-7bc4-9f45-6e6c32a19484")]
    assert "deleted run" in result.output
