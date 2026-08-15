"""The gate: `tracewake replay <id>` reproduces a recorded run with no network.

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

import tracewake
from tracewake import Store
from tracewake.config import Config
from tracewake.redaction import Redactor


def _scrubbed(parts: list[str]) -> list[str]:
    redactor = Redactor(Config(redact=True))
    return [redactor.text(part) for part in parts]

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


def _tracewake(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tracewake", *args],
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
        "TRACEWAKE_TEST_PORT": str(server.server_address[1]),
        "TRACEWAKE_TEST_TRANSCRIPT": str(tmp_path / "transcript.txt"),
        "TRACEWAKE_TEST_TAG": "gate-tag",
    }


def test_replay_reproduces_the_run_with_the_network_disabled(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    transcript = Path(env["TRACEWAKE_TEST_TRANSCRIPT"])

    recorded = _tracewake(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    assert server.connections == len(SCRIPT), "the recorded run was meant to use the network"
    recorded_transcript = transcript.read_text(encoding="utf-8")
    transcript.unlink()

    run_id = Store(store).latest_named("gate").run_id
    server.reset()

    replayed = _tracewake("replay", run_id, "--store", str(store), env=env)
    assert replayed.returncode == 0, replayed.stderr
    assert transcript.read_text(encoding="utf-8") == recorded_transcript
    assert server.connections == 0, "replay reached the network"


def test_a_network_call_during_replay_fails_the_run(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    """The negative control: without it, the count above could be zero by luck."""
    store = tmp_path / "store"
    recorded = _tracewake(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    run_id = Store(store).latest_named("gate").run_id
    server.reset()

    with tracewake.replay(run_id, store=store):
        with pytest.raises(tracewake.NetworkBlocked, match="network call was attempted"):
            socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    assert server.connections == 0


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda sock: sock.send(b"blocked"), id="send"),
        pytest.param(lambda sock: sock.sendall(b"blocked"), id="sendall"),
        pytest.param(
            lambda sock: sock.sendto(b"blocked", ("127.0.0.1", 9)), id="sendto"
        ),
        pytest.param(
            lambda sock: sock.sendmsg([b"blocked"], [], 0, ("127.0.0.1", 9)),
            id="sendmsg",
            marks=pytest.mark.skipif(
                not hasattr(socket.socket, "sendmsg"), reason="sendmsg is unavailable"
            ),
        ),
    ],
)
def test_every_socket_send_surface_is_blocked_during_replay(
    tmp_path: Path, send
) -> None:
    with tracewake.record("send-gate", store=tmp_path) as rec:
        rec.outcome(status="ok")
        run_id = rec.run_id

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with tracewake.replay(run_id, store=tmp_path):
            with pytest.raises(tracewake.NetworkBlocked):
                send(sock)


def test_the_replay_gate_exposes_no_off_switch(tmp_path: Path) -> None:
    """What keeps the gate from being configured away is that there is nothing to
    configure: replay consults no setting before blocking, so asking for the
    setting is an error rather than a request that gets quietly ignored."""
    with pytest.raises(TypeError, match="block_network"):
        Config(block_network=False)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="block_network"):
        with tracewake.replay("gate", store=tmp_path, block_network=False):
            pass


def test_once_mode_is_blocked_when_it_resolves_to_pure_replay(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    recorded = _tracewake(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    server.reset()

    with tracewake.session("gate", store=store, mode="once"):
        with pytest.raises(tracewake.NetworkBlocked):
            socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    assert server.connections == 0


def test_the_socket_block_is_lifted_when_the_session_ends(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    _tracewake("record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env)
    run_id = Store(store).latest_named("gate").run_id
    with tracewake.replay(run_id, store=store):
        pass

    server.reset()
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(b"0\n")
        # Reading the reply also synchronizes with the handler thread, which
        # counts the connection before it answers.
        assert sock.makefile("r").readline()
    assert server.connections == 1


def test_an_intervention_replays_the_prefix_and_pays_only_for_the_rest(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    """A fork's cost is the inference after the change, not the whole run."""
    store = tmp_path / "store"
    recorded = _tracewake(
        "record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env
    )
    assert recorded.returncode == 0, recorded.stderr
    source = Store(store).latest_named("gate").run_id
    server.reset()

    forked = _tracewake(
        "intervene", source, "--store", str(store),
        "--drop-tag", "user_task", "--from-step", "1",
        "--", sys.executable, str(AGENT),
        env=env,
    )
    assert forked.returncode == 0, forked.stderr
    # Turn 0 still matched the recorded call and never left the log; only the
    # turn whose context changed reached the model.
    assert server.connections == 1
    out = forked.stdout
    assert "block" in out and "dropping" in out
    assert "matched" in out
    assert f"tracewake diff {source}" in out and "--store" in out

    db = Store(store)
    runs = {h.run_id for h in db.runs()}
    fork = next(h for h in db.runs() if h.run_id != source)
    declared = [e.event for e in db.events(fork.run_id) if e.event.type == "intervention"]
    kept = [
        m
        for e in db.events(fork.run_id)
        if e.event.type == "model_call"
        for m in e.event.messages
    ]
    db.close()

    assert len(runs) == 2
    assert declared and declared[0].source_run_id == source
    assert fork.name == "gate+drop-user_task@1"
    assert not [m for m in kept if m.provenance == "user_task"]
    assert fork.run_id in out


def test_an_intervention_that_changes_nothing_is_refused_by_the_cli(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    _tracewake("record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env)
    source = Store(store).latest_named("gate").run_id
    server.reset()

    refused = _tracewake(
        "intervene", source, "--store", str(store), "--drop-tag", "repo_map",
        "--", sys.executable, str(AGENT),
        env=env,
    )
    assert refused.returncode != 0
    assert "repo_map" in refused.stderr + refused.stdout
    assert server.connections == 0, "it spent inference before finding out"
    assert len(Store(store).runs()) == 1, "it created a run it could not use"


def test_recording_through_the_cli_captures_the_whole_environment(
    server: _Server, tmp_path: Path, env: dict[str, str]
) -> None:
    store = tmp_path / "store"
    _tracewake("record", "--store", str(store), "--name", "gate", "--", sys.executable, str(AGENT), env=env)

    db = Store(store)
    header = db.latest_named("gate")
    events = db.events(header.run_id)
    kinds = {e.event.type for e in events}
    sources = {e.event.source for e in events if e.event.type == "environment"}
    db.close()

    assert {"model_call", "tool_call", "environment", "fs_read", "outcome"} <= kinds
    assert {"clock", "random", "uuid", "env"} <= sources
    assert header.command == _scrubbed([sys.executable, str(AGENT)])
    assert [m.model_id for m in header.models] == ["testnet-1"]
