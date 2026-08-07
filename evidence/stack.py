"""A disposable Locus deployment: PostgreSQL, the control plane, the worker.

The measurements this package publishes have to come from processes that can
be killed, restarted, and starved, so the stack is driven directly rather than
through the documented single command.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED = ("go", "initdb", "pg_ctl", "pg_isready", "createdb", "psql", "pg_dump")
# PostgreSQL refuses a socket directory longer than 103 bytes, which a
# scratch directory easily exceeds.
SOCKET_ROOT = Path("/tmp/locus-evidence")


class StackError(RuntimeError):
    pass


def _wait(condition: Any, seconds: float, description: str, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise StackError(f"timed out waiting for {description}")


@dataclass
class Stack:
    root: Path
    repository: Path
    postgres_port: int = 55440
    public_port: int = 8090
    worker_port: int = 8091
    metric_interval: str = "2s"

    postgres_socket: Path = field(init=False)
    control_plane: subprocess.Popen[bytes] | None = field(default=None, init=False)
    worker: subprocess.Popen[bytes] | None = field(default=None, init=False)
    credentials: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        missing = [command for command in REQUIRED if shutil.which(command) is None]
        if missing:
            raise StackError(f"missing required commands: {', '.join(missing)}")
        self.root.mkdir(parents=True, exist_ok=True)
        SOCKET_ROOT.mkdir(parents=True, exist_ok=True)
        self.postgres_socket = SOCKET_ROOT / f"pg{self.postgres_port}"
        self.postgres_socket.mkdir(exist_ok=True)
        self.telemetry_dir = self.root / "telemetry"
        self.telemetry_dir.mkdir(exist_ok=True)

    @property
    def data_dir(self) -> Path:
        return self.root / "postgres"

    @property
    def database_url(self) -> str:
        return f"postgres://locus@localhost/locus?host={self.postgres_socket}&port={self.postgres_port}&sslmode=disable"

    @property
    def public_url(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"

    @property
    def worker_url(self) -> str:
        return f"http://127.0.0.1:{self.worker_port}"

    def start_postgres(self) -> None:
        if not (self.data_dir / "PG_VERSION").exists():
            subprocess.run(
                ["initdb", "-D", str(self.data_dir), "-A", "trust", "-U", "locus", "--no-locale"],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            [
                "pg_ctl", "-D", str(self.data_dir), "-l", str(self.root / "postgres.log"),
                "-o", f"-p {self.postgres_port} -k {self.postgres_socket} -c listen_addresses=", "start",
            ],
            check=True,
            capture_output=True,
        )
        _wait(self.postgres_ready, 30, "PostgreSQL")
        if not self.psql("SELECT 1 FROM pg_database WHERE datname='locus'", database="postgres").strip():
            subprocess.run(
                ["createdb", "-h", str(self.postgres_socket), "-p", str(self.postgres_port), "-U", "locus", "locus"],
                check=True,
                capture_output=True,
            )

    def postgres_ready(self) -> bool:
        return subprocess.run(
            ["pg_isready", "-h", str(self.postgres_socket), "-p", str(self.postgres_port), "-U", "locus"],
            capture_output=True,
        ).returncode == 0

    def stop_postgres(self, mode: str = "fast") -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data_dir), "-m", mode, "stop"], capture_output=True)

    def psql(self, statement: str, database: str = "locus", allow_failure: bool = False) -> str:
        result = subprocess.run(
            [
                "psql", "-h", str(self.postgres_socket), "-p", str(self.postgres_port),
                "-U", "locus", "-d", database, "-tAc", statement,
            ],
            capture_output=True,
            text=True,
            check=not allow_failure,
        )
        return result.stdout

    def build(self) -> Path:
        binary = self.root / "locusd"
        subprocess.run(
            ["go", "build", "-o", str(binary), "./cmd/locusd"],
            cwd=self.repository / "controlplane",
            check=True,
            capture_output=True,
        )
        return binary

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "LOCUS_DATABASE_URL": self.database_url,
                "LOCUS_ARTIFACT_ROOT": str(self.root / "artifacts"),
                "LOCUS_LISTEN_ADDR": f"127.0.0.1:{self.public_port}",
                "LOCUS_WORKER_LISTEN_ADDR": f"127.0.0.1:{self.worker_port}",
                "LOCUS_BOOTSTRAP_FILE": str(self.root / "credentials.json"),
                "LOCUS_TOKEN_PEPPER": self._secret("tenant"),
                "LOCUS_WORKER_PEPPER": self._secret("worker"),
                "LOCUS_ENVIRONMENT": "evidence",
                "LOCUS_SERVICE_VERSION": "evidence",
                "LOCUS_TELEMETRY_INTERVAL": self.metric_interval,
            }
        )
        return environment

    def _secret(self, name: str) -> str:
        path = self.root / f"{name}.pepper"
        if not path.exists():
            path.write_text(secrets.token_hex(32), encoding="utf-8")
            path.chmod(0o600)
        return path.read_text(encoding="utf-8")

    def start_control_plane(self) -> None:
        binary = self.root / "locusd"
        if not binary.exists():
            self.build()
        stream = open(self.telemetry_dir / "control-plane.jsonl", "ab")
        errors = open(self.root / "control-plane.err", "ab")
        self.control_plane = subprocess.Popen(
            [str(binary)], env=self._environment(), stdout=stream, stderr=errors, start_new_session=True
        )
        _wait(self.healthy, 60, "the control plane")
        self.credentials = json.loads((self.root / "credentials.json").read_text(encoding="utf-8"))

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.public_url + "/healthz", timeout=2) as response:
                return bool(response.status in (200, 204))
        except (urllib.error.URLError, OSError):
            return False

    def stop_control_plane(self, kill: bool = False) -> None:
        self.control_plane = _terminate(self.control_plane, kill)

    def start_worker(self, build: str = "evidence") -> None:
        environment = dict(os.environ)
        environment.update(
            {
                "LOCUS_WORKER_URL": self.worker_url,
                "LOCUS_WORKER_CREDENTIALS_FILE": str(self.root / "credentials.json"),
                "LOCUS_WORKER_BUILD": build,
                "LOCUS_ENVIRONMENT": "evidence",
                "LOCUS_SERVICE_VERSION": "evidence",
                "PYTHONUNBUFFERED": "1",
            }
        )
        stream = open(self.telemetry_dir / "worker.jsonl", "ab")
        errors = open(self.root / "worker.err", "ab")
        # The launcher is a wrapper around the real worker, so the whole
        # process group has to go: killing the wrapper alone would leave a
        # worker that keeps heartbeating and never loses its lease.
        self.worker = subprocess.Popen(
            ["uv", "run", "--project", str(self.repository), "locus-worker"],
            env=environment,
            stdout=stream,
            stderr=errors,
            start_new_session=True,
        )

    def stop_worker(self, kill: bool = True) -> None:
        self.worker = _terminate(self.worker, kill)

    def stop(self) -> None:
        self.stop_worker()
        self.stop_control_plane()
        self.stop_postgres()

    def dump(self, destination: Path) -> Path:
        subprocess.run(
            [
                "pg_dump", "-h", str(self.postgres_socket), "-p", str(self.postgres_port),
                "-U", "locus", "-d", "locus", "-Fc", "-f", str(destination),
            ],
            check=True,
            capture_output=True,
        )
        return destination

    def restore(self, source: Path) -> None:
        subprocess.run(
            [
                "psql", "-h", str(self.postgres_socket), "-p", str(self.postgres_port),
                "-U", "locus", "-d", "postgres", "-c", "DROP DATABASE locus WITH (FORCE)",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["createdb", "-h", str(self.postgres_socket), "-p", str(self.postgres_port), "-U", "locus", "locus"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "pg_restore", "-h", str(self.postgres_socket), "-p", str(self.postgres_port),
                "-U", "locus", "-d", "locus", str(source),
            ],
            check=True,
            capture_output=True,
        )


def _terminate(process: subprocess.Popen[bytes] | None, kill: bool) -> None:
    if process is None or process.poll() is not None:
        return None
    number = signal.SIGKILL if kill else signal.SIGTERM
    try:
        os.killpg(os.getpgid(process.pid), number)
    except (ProcessLookupError, PermissionError):
        process.send_signal(number)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=5)
    return None


def wait_for(condition: Any, seconds: float, description: str, interval: float = 0.2) -> Any:
    return _wait(condition, seconds, description, interval)
