"""The measured scenarios. Every published number is produced here."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from tracewake.worker import WorkerClient

from .client import ApiError, Client, put_object
from .stack import Stack, wait_for

TERMINAL = {"succeeded", "failed", "cancelled"}


def server_ms(start: str, end: str) -> float:
    """Elapsed time between two API timestamps.

    Durations come from the database rather than from when this process
    happened to poll, so a slow observer cannot inflate a measurement.
    """
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000


def wait_for_job(client: Client, job_id: str, timeout: float = 240) -> dict[str, Any]:
    return wait_for(
        lambda: (job := client.job(job_id)) and job["state"] in TERMINAL and job,
        timeout,
        f"job {job_id} to reach a terminal state",
    )


def wait_for_run(client: Client, run_id: str, timeout: float = 240) -> dict[str, Any]:
    return wait_for(
        lambda: (run := client.run(run_id)) and run["state"] in {"ready", "invalid"} and run,
        timeout,
        f"run {run_id} to finish validation",
    )


def ingestion(client: Client, bundles: list[bytes]) -> dict[str, Any]:
    """Upload bundles and measure the mandatory validation that gates them."""
    observed: list[float] = []
    validated: list[float] = []
    run_ids: list[str] = []
    for bundle in bundles:
        started = time.perf_counter()
        run_id = client.upload(bundle)
        run = wait_for_run(client, run_id)
        observed.append((time.perf_counter() - started) * 1000)
        if run["state"] != "ready":
            raise RuntimeError(f"run {run_id} did not become ready: {run}")
        validated.append(server_ms(run["created_at"], run["ready_at"]))
        run_ids.append(run_id)
    return {
        "run_ids": run_ids,
        "observed_upload_to_ready_ms": observed,
        "upload_to_ready_ms": validated,
        "bundle_bytes": [len(bundle) for bundle in bundles],
    }


def analysis_load(client: Client, pairs: Iterator[tuple[str, str]], count: int, prefix: str) -> dict[str, Any]:
    """Submit a batch of analyses at once and measure how the system drains it.

    Normalized job inputs are unique per workspace, so every job analyses a
    different pair of runs and does real work rather than replaying one result.
    """
    requests = [(next(pairs), f"{prefix}-{index}") for index in range(count)]
    started = time.perf_counter()
    jobs = [client.analyze("diff", [a, b], key, "align-v1")["job_id"] for (a, b), key in requests]
    submitted = time.perf_counter()
    latencies: list[float] = []
    outcomes: dict[str, int] = {}
    for job_id in jobs:
        job = wait_for_job(client, job_id)
        outcomes[job["state"]] = outcomes.get(job["state"], 0) + 1
        latencies.append(server_ms(job["created_at"], job["terminal_at"]))
    elapsed = time.perf_counter() - started
    return {
        "jobs": count,
        "submit_seconds": round(submitted - started, 3),
        "drain_seconds": round(elapsed, 3),
        "jobs_per_minute": round(count / elapsed * 60, 2),
        "create_to_terminal_ms": latencies,
        "outcomes": outcomes,
        "job_ids": jobs,
        "first_request": {"run_ids": list(requests[0][0]), "idempotency_key": requests[0][1]},
    }


def soak(client: Client, pairs: Iterator[tuple[str, str]], seconds: float, interval: float, prefix: str) -> dict[str, Any]:
    """Hold a steady submission rate and check the system does not drift."""
    deadline = time.monotonic() + seconds
    submitted: list[tuple[str, float]] = []
    index = 0
    while time.monotonic() < deadline:
        first, second = next(pairs)
        job = client.analyze("diff", [first, second], f"{prefix}-{index}", "align-v1")
        submitted.append((job["job_id"], time.perf_counter()))
        index += 1
        time.sleep(interval)
    latencies: list[float] = []
    outcomes: dict[str, int] = {}
    for job_id, _ in submitted:
        job = wait_for_job(client, job_id)
        outcomes[job["state"]] = outcomes.get(job["state"], 0) + 1
        latencies.append(server_ms(job["created_at"], job["terminal_at"]))
    half = len(latencies) // 2
    return {
        "duration_seconds": round(seconds, 1),
        "submission_interval_seconds": interval,
        "jobs": len(submitted),
        "outcomes": outcomes,
        "create_to_terminal_ms": latencies,
        "first_half_mean_ms": round(sum(latencies[:half]) / half, 3) if half else None,
        "second_half_mean_ms": round(sum(latencies[half:]) / (len(latencies) - half), 3) if half else None,
    }


def worker_recovery(stack: Stack, client: Client, runs: tuple[str, str], key: str) -> dict[str, Any]:
    """Kill the worker holding an attempt and follow the job to one result.

    The runs analysed here are deliberately large: the kill has to land while
    the attempt is genuinely in flight, not after it has already committed.
    """
    job_id = client.analyze("diff", [runs[0], runs[1]], key, "align-v1")["job_id"]
    # The database answers far faster than the API, which matters when the
    # window between claim and commit is what is being interrupted.
    wait_for(
        lambda: stack.psql(f"SELECT state FROM job_attempts WHERE job_id='{job_id}' AND attempt_number=1").strip() == "running",
        60,
        "the first attempt to start",
        interval=0.02,
    )
    progress = client.job(job_id)["progress"]
    killed = time.perf_counter()
    stack.stop_worker(kill=True)
    if stack.psql(f"SELECT state FROM jobs WHERE id='{job_id}'").strip() != "running":
        raise RuntimeError("the attempt finished before the worker could be killed")
    fenced = wait_for(
        lambda: next((a for a in client.job(job_id)["attempts"] if a["attempt_number"] == 1 and a["state"] == "fenced"), None),
        180,
        "the abandoned attempt to be fenced",
        interval=0.5,
    )
    fenced_at = time.perf_counter()
    stack.start_worker()
    job = wait_for_job(client, job_id, timeout=240)
    recovered_at = time.perf_counter()
    attempts = job["attempts"]
    return {
        "job_id": job_id,
        "state": job["state"],
        "attempts": [{"attempt_number": a["attempt_number"], "state": a["state"], "failure": a["failure"]} for a in attempts],
        "fence_reason": fenced["failure"]["code"] if fenced["failure"] else None,
        "kill_to_fence_seconds": round(fenced_at - killed, 3),
        "kill_to_success_seconds": round(recovered_at - killed, 3),
        "succeeded_attempts": sum(1 for a in attempts if a["state"] == "succeeded"),
        "result_artifacts": len(job["artifacts"]),
        "progress_while_running": progress,
    }


def _worker_client(stack: Stack) -> WorkerClient:
    return WorkerClient(stack.worker_url, stack.credentials["worker_id"], stack.credentials["worker_token"])


def _claim(stack: Stack, worker: WorkerClient, job_id: str, operation: str, timeout: float = 120) -> dict[str, Any]:
    """Claim one named job.

    Several of these scenarios leave notifications behind on purpose, so a
    worker that took whichever message arrived next would test a different job
    than the one under measurement.
    """
    wait_for(
        lambda: stack.psql(f"SELECT state FROM jobs WHERE id='{job_id}'").strip() == "queued",
        timeout,
        f"job {job_id} to become claimable",
        interval=0.5,
    )
    version = int(stack.psql(f"SELECT row_version FROM jobs WHERE id='{job_id}'").strip())
    return dict(
        worker.json(
            "POST", "/internal/v1/claims",
            {
                "protocol_version": 1,
                "worker_id": worker.worker_id,
                "notification": {"protocol_version": 1, "job_id": job_id, "job_version": version, "operation": operation},
            },
        )
    )


def late_completion(stack: Stack, client: Client, runs: tuple[str, str], key: str) -> dict[str, Any]:
    """Take an attempt, stop heartbeating, and try to commit after fencing."""
    worker = _worker_client(stack)
    job_id = client.analyze("diff", [runs[0], runs[1]], key, "align-v1")["job_id"]
    token = _claim(stack, worker, job_id, "diff")["attempt_token"]
    wait_for(
        lambda: any(a["attempt_number"] == 1 and a["state"] == "fenced" for a in client.job(job_id)["attempts"]),
        180,
        "the silent attempt to be fenced",
        interval=0.5,
    )
    rejections = {}
    for name, method, path, body in (
        ("heartbeat", "PUT", f"/internal/v1/jobs/{job_id}/attempts/1/heartbeat",
         {"protocol_version": 1, "attempt_number": 1, "observed_lease_expires_at": "1970-01-01T00:00:00Z"}),
        ("progress", "PUT", f"/internal/v1/jobs/{job_id}/attempts/1/progress",
         {"protocol_version": 1, "attempt_number": 1, "sequence": 9, "stage": "committing", "message": "late progress"}),
        ("complete", "POST", f"/internal/v1/jobs/{job_id}/attempts/1/complete",
         {"artifact_id": "", "kind": "diff_json", "object_key": f"workspaces/x/jobs/{job_id}/attempts/1/diff_json",
          "object_version": "v", "digest": "0" * 64, "media_type": "application/json",
          "schema_name": "result-envelope", "size": 2, "schema_version": 1, "logical_run_digest": "",
          "bundle_digest": "", "event_count": 0, "bundle_format_version": 0, "cassette_format_version": 0,
          "event_schema_version": 0, "companions": []}),
    ):
        try:
            worker.json(method, path, body, attempt_token=token)
            rejections[name] = "accepted"
        except Exception as exc:  # the worker client raises LeaseLost for HTTP 409
            rejections[name] = type(exc).__name__
    return {"job_id": job_id, "stale_attempt_requests": rejections}


def retry_exhaustion(stack: Stack, client: Client, runs: tuple[str, str], key: str) -> dict[str, Any]:
    """Fail every allowed attempt retryably and watch the job go terminal."""
    worker = _worker_client(stack)
    job_id = client.analyze("diff", [runs[0], runs[1]], key, "align-v1")["job_id"]
    states = []
    for _ in range(3):
        claim = _claim(stack, worker, job_id, "diff", timeout=180)
        response = worker.json(
            "POST", f"/internal/v1/jobs/{job_id}/attempts/{claim['attempt_number']}/fail",
            {"schema_version": 1, "code": "internal", "message": "injected fault", "retryable": True},
            attempt_token=claim["attempt_token"],
        )
        states.append(response["state"])
    job = wait_for_job(client, job_id, timeout=180)
    return {
        "job_id": job_id,
        "attempt_states": states,
        "state": job["state"],
        "failure_code": job["failure"]["code"] if job["failure"] else None,
        "artifacts": len(job["artifacts"]),
    }


def artifact_mismatch(stack: Stack, client: Client, run_id: str, key: str) -> dict[str, Any]:
    """Contradict an artifact declaration at both boundaries that check it.

    The store refuses bytes that disagree with the declaration it signed, and
    the commit refuses an identity that disagrees with what the store holds.
    Neither refusal may leave a successful job pointing at the object.
    """
    worker = _worker_client(stack)
    job_id = client.analyze("otlp", [run_id], key)["job_id"]
    claim = _claim(stack, worker, job_id, "otlp")
    attempt = claim["attempt_number"]
    declared = b'{"declared":true}'
    other = b'{"declared":false}'

    def grant_for(payload: bytes) -> dict[str, Any]:
        return dict(
            worker.json(
                "POST", f"/internal/v1/jobs/{job_id}/attempts/{attempt}/artifacts",
                {"protocol_version": 1, "attempt_number": attempt, "kind": "otlp_result_json",
                 "media_type": "application/json", "digest": hashlib.sha256(payload).hexdigest(), "size": len(payload)},
                attempt_token=claim["attempt_token"],
            )
        )

    grant = grant_for(declared)
    upload = "accepted"
    try:
        put_object(grant["upload_url"], grant.get("upload_headers") or {}, other)
    except urllib.error.HTTPError as exc:
        upload = f"HTTP {exc.code}"

    version = put_object(grant["upload_url"], grant.get("upload_headers") or {}, declared)
    commit = "accepted"
    try:
        worker.json(
            "POST", f"/internal/v1/jobs/{job_id}/attempts/{attempt}/complete",
            {"artifact_id": "", "kind": "otlp_result_json", "object_key": grant["object_key"],
             "object_version": version, "digest": hashlib.sha256(other).hexdigest(),
             "media_type": "application/json", "schema_name": "result-envelope", "size": len(other),
             "schema_version": 1, "logical_run_digest": "", "bundle_digest": "", "event_count": 0,
             "bundle_format_version": 0, "cassette_format_version": 0, "event_schema_version": 0, "companions": []},
            attempt_token=claim["attempt_token"],
        )
    except Exception as exc:
        commit = type(exc).__name__

    worker.json(
        "POST", f"/internal/v1/jobs/{job_id}/attempts/{attempt}/fail",
        {"schema_version": 1, "code": "artifact_commit_failed", "message": "stored bytes did not match", "retryable": True},
        attempt_token=claim["attempt_token"],
    )
    job = client.job(job_id)
    return {
        "job_id": job_id,
        "contradicting_upload": upload,
        "contradicting_commit": commit,
        "state": job["state"],
        "artifacts": len(job["artifacts"]),
    }


def outbox_backlog(stack: Stack, client: Client, run_id: str, key: str, seconds: float) -> dict[str, Any]:
    """With nothing consuming notifications, the backlog has to become visible."""
    job_id = client.analyze("pprof", [run_id], key)["job_id"]
    time.sleep(seconds)
    age = float(stack.psql(
        f"SELECT COALESCE(EXTRACT(EPOCH FROM (transaction_timestamp()-min(created_at))),0) FROM outbox WHERE published_at IS NULL AND aggregate_id='{job_id}'"
    ).strip() or 0)
    oldest = float(stack.psql(
        "SELECT COALESCE(EXTRACT(EPOCH FROM (transaction_timestamp()-min(created_at))),0) FROM outbox WHERE published_at IS NULL"
    ).strip() or 0)
    return {"job_id": job_id, "unconsumed_seconds": round(age, 3), "oldest_unpublished_seconds": round(oldest, 3)}


def reconciler_failure(stack: Stack, seconds: float) -> dict[str, Any]:
    """Take the database away and confirm the reconciler reports it."""
    stack.stop_postgres(mode="immediate")
    time.sleep(seconds)
    stack.start_postgres()
    wait_for(stack.healthy, 60, "the control plane after the database returned")
    return {"database_down_seconds": round(seconds, 1)}


def tenant_isolation(stack: Stack, client: Client, other_token: str, run_id: str, job_id: str) -> dict[str, Any]:
    """A second workspace must not observe the first workspace's records."""
    other = Client(stack.public_url, other_token)
    unknown = Client(stack.public_url, "tracewake_0000000000000000.notatoken")
    observations = {
        "runs_visible": len(other.request("GET", "/v1/runs")["runs"]),
        "audit_visible": len(other.audit()),
    }
    for name, path in (("run", f"/v1/runs/{run_id}"), ("job", f"/v1/jobs/{job_id}")):
        try:
            other.request("GET", path)
            observations[name] = "visible"
        except ApiError as exc:
            observations[name] = f"HTTP {exc.status}"
    try:
        unknown.request("GET", "/v1/runs")
        observations["unknown_token"] = "accepted"
    except ApiError as exc:
        observations["unknown_token"] = f"HTTP {exc.status}"
    return observations


def backup_and_restore(stack: Stack, client: Client, run_id: str, job_id: str) -> dict[str, Any]:
    """Prove authoritative state survives a dump and reload."""
    dump = stack.dump(stack.root / "tracewake.dump")
    before = {
        "runs": int(stack.psql("SELECT count(*) FROM runs")),
        "jobs": int(stack.psql("SELECT count(*) FROM jobs")),
        "artifacts": int(stack.psql("SELECT count(*) FROM artifacts")),
        "audit": int(stack.psql("SELECT count(*) FROM audit_records")),
    }
    stack.stop_control_plane()
    stack.restore(dump)
    stack.start_control_plane()
    after = {
        "runs": int(stack.psql("SELECT count(*) FROM runs")),
        "jobs": int(stack.psql("SELECT count(*) FROM jobs")),
        "artifacts": int(stack.psql("SELECT count(*) FROM artifacts")),
        "audit": int(stack.psql("SELECT count(*) FROM audit_records")),
    }
    return {
        "dump_bytes": dump.stat().st_size,
        "before": before,
        "after": after,
        "run_still_ready": client.run(run_id)["state"] == "ready",
        "job_still_terminal": client.job(job_id)["state"] in TERMINAL,
    }


def migration(stack: Stack) -> dict[str, Any]:
    """Migrate an empty database, then confirm a second pass changes nothing."""
    subprocess.run(
        ["createdb", "-h", str(stack.postgres_socket), "-p", str(stack.postgres_port), "-U", "tracewake", "tracewake_migration"],
        capture_output=True,
    )
    environment = stack._environment()
    environment["TRACEWAKE_DATABASE_URL"] = stack.database_url.replace("/tracewake?", "/tracewake_migration?")
    environment["TRACEWAKE_LISTEN_ADDR"] = "127.0.0.1:8098"
    environment["TRACEWAKE_WORKER_LISTEN_ADDR"] = "127.0.0.1:8099"
    environment["TRACEWAKE_BOOTSTRAP_FILE"] = str(stack.root / "migration-credentials.json")
    versions = []
    for _ in range(2):
        process = subprocess.Popen(
            [str(stack.root / "tracewaked")], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            wait_for(
                lambda: stack.psql(
                    "SELECT count(*) FROM schema_migrations", database="tracewake_migration", allow_failure=True
                ).strip() not in ("", "0"),
                60,
                "the schema ledger",
            )
            versions.append(
                [int(line) for line in stack.psql("SELECT version FROM schema_migrations ORDER BY version", database="tracewake_migration").split()]
            )
        finally:
            process.terminate()
            process.wait(timeout=15)
    applied = stack.psql(
        "SELECT count(*) FROM schema_migrations WHERE applied_at > transaction_timestamp() - interval '1 hour'",
        database="tracewake_migration",
    )
    return {
        "applied_versions": versions[0],
        "second_pass_versions": versions[1],
        "reapplied": versions[0] != versions[1],
        "ledger_rows": int(applied),
    }


def local_independence(repository: Path, root: Path) -> dict[str, Any]:
    """Local Tracewake must work with no hosted service reachable at all."""
    store = root / "independent-store"
    record = subprocess.run(
        ["uv", "run", "--project", str(repository), "tracewake", "record", "--store", str(store), "--name", "offline",
         "--", "python", "-c",
         "import tracewake; s = tracewake.current(); print(s.clock.time()); s.outcome(status='ok')"],
        capture_output=True, text=True, check=True,
    )
    replay = subprocess.run(
        ["uv", "run", "--project", str(repository), "tracewake", "replay", "offline", "--store", str(store)],
        capture_output=True, text=True, check=True,
    )
    return {
        "recorded_first_line": record.stdout.splitlines()[0],
        "replayed_first_line": replay.stdout.splitlines()[0],
        "identical": record.stdout.splitlines()[0] == replay.stdout.splitlines()[0],
    }


def hosted_matches_local(client: Client, job: dict[str, Any], bundle: bytes, root: Path) -> dict[str, Any]:
    """The hosted result has to agree with running the same analysis locally."""
    from tracewake.bundle import bundle_header, validate_bundle
    from tracewake.otel import encode_spans

    path = root / "agreement.tar"
    path.write_bytes(bundle)
    validated = validate_bundle(path)
    local, spans = encode_spans(bundle_header(validated), list(validated.events))
    artifact = next(item for item in job["artifacts"] if item["kind"] == "otlp_json")
    hosted = client.download(artifact["artifact_id"])
    envelope = json.loads(client.download(next(i for i in job["artifacts"] if i["kind"] == "otlp_result_json")["artifact_id"]))
    return {
        "hosted_digest": hashlib.sha256(hosted).hexdigest(),
        "local_digest": hashlib.sha256(local).hexdigest(),
        "identical_bytes": hosted == local,
        "hosted_span_count": envelope["result"]["span_count"],
        "local_span_count": spans,
    }


def result_provenance(client: Client, job_id: str) -> dict[str, Any]:
    """Read back everything a finished analysis is supposed to account for."""
    job = client.job(job_id)
    artifacts = {item["kind"]: item for item in job["artifacts"]}
    envelope = json.loads(client.download(artifacts["diff_json"]["artifact_id"]))
    provenance = envelope["result"]["provenance"]
    companion = client.download(artifacts["diff_html"]["artifact_id"])
    audit = [record["event_type"] for record in client.audit() if record["aggregate_id"] == job_id]
    return {
        "job_id": job_id,
        "artifact_kinds": sorted(artifacts),
        "downloads_match_recorded_identity": all(
            hashlib.sha256(client.download(item["artifact_id"])).hexdigest() == item["digest"]
            and len(client.download(item["artifact_id"])) == item["size"]
            for item in job["artifacts"]
        ),
        "html_is_self_contained": b"<html" in companion.lower() and b"http://" not in companion,
        "analysis_profile": provenance["analysis_profile"],
        "tracewake_version": provenance["tracewake_version"],
        "worker_build": provenance["worker_build"],
        "input_digests": [
            {
                "logical_run_digest": item["logical_run_digest"],
                "bundle_digest": item["bundle_digest"],
                "bundle_object_version": item["bundle_object_version"],
                "event_schema_version": item["event_schema_version"],
                "cassette_format_version": item["cassette_format_version"],
                "bundle_format_version": item["bundle_format_version"],
            }
            for item in provenance["inputs"]
        ],
        "result_schema": {
            "name": artifacts["diff_json"]["schema_name"],
            "version": artifacts["diff_json"]["schema_version"],
        },
        "audit_events": audit,
    }
