"""The gate: `locus replay <id>` reproduces a recorded run with no network.

The agent under test reaches a real socket for every model call. The server
counts connections, so a replay that touched the network fails this test twice
over: the count is non-zero, and the blocked connect crashes the child.
"""

from __future__ import annotations

import json
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import locus
from locus import Store

AGENT = Path(__file__).parent / "net_agent.py"

SCRIPT = [
    {
        "text": "Reading the module before I summarize it.",
        "tool_calls": [{"id": "call_0", "name": "read_file", "args": {"path": "net_agent.py"}}],
        "finish_reason": "tool_use",
        "input_tokens": 40,
    },
    {
        "text": "It records a run and replays it.",
        "tool_calls": [],
        "finish_reason": "end_turn",
        "input_tokens": 80,
    },
]


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        with server.lock:
            server.connections += 1
            index = min(server.served, len(SCRIPT) - 1)
            server.served += 1
        self.rfile.readline()
        self.wfile.write((json.dumps(SCRIPT[index]) + "\n").encode("utf-8"))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self.served = 0
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.connections = 0
            self.served = 0


@pytest.fixture
def server() -> _Server:
    srv = _Server()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _locus(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "locus", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def env(server: _Server, tmp_path: Path) -> dict[str, str]:
    import os

    return {
        **os.environ,
        "LOCUS_TEST_PORT": str(server.server_address[1]),
        "LOCUS_TEST_TRANSCRIPT": str(tmp_path / "transcript.txt"),
        "LOCUS_TEST_TAG": "gate-tag",
    }


def test_replay_reproduces_the_run_with_the_network_disabled(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    transcript = Path(env["LOCUS_TEST_TRANSCRIPT"])

    recorded = _locus(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    assert server.connections == len(SCRIPT), "the recorded run was meant to use the network"
    recorded_transcript = transcript.read_text(encoding="utf-8")
    transcript.unlink()

    run_id = Store(store).latest_named("gate").run_id
    server.reset()

    replayed = _locus("replay", run_id, "--store", str(store), env=env)
    assert replayed.returncode == 0, replayed.stderr
    assert transcript.read_text(encoding="utf-8") == recorded_transcript
    assert server.connections == 0, "replay reached the network"


def test_a_network_call_during_replay_fails_the_run(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    """The negative control: without it, the count above could be zero by luck."""
    store = tmp_path / "store"
    recorded = _locus(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    run_id = Store(store).latest_named("gate").run_id
    server.reset()

    with locus.replay(run_id, store=store):
        with pytest.raises(locus.NetworkBlocked, match="network call was attempted"):
            socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    assert server.connections == 0


def test_the_socket_block_is_lifted_when_the_session_ends(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env)
    run_id = Store(store).latest_named("gate").run_id
    with locus.replay(run_id, store=store):
        pass

    server.reset()
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(b"0\n")
        # Reading the reply also synchronizes with the handler thread, which
        # counts the connection before it answers.
        assert sock.makefile("r").readline()
    assert server.connections == 1


def test_recording_through_the_cli_captures_the_whole_environment(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    _locus("record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env)

    db = Store(store)
    header = db.latest_named("gate")
    events = db.events(header.run_id)
    kinds = {e.event.type for e in events}
    sources = {e.event.source for e in events if e.event.type == "environment"}
    db.close()

    assert {"model_call", "tool_call", "environment", "fs_read", "outcome"} <= kinds
    assert {"clock", "random", "uuid", "env"} <= sources
    assert header.command == [sys.executable, str(AGENT)]
    assert [m.model_id for m in header.models] == ["testnet-1"]
