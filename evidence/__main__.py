"""Run every measured scenario against a disposable deployment.

    uv run python -m evidence --output evidence/results

Nothing here reads an AWS account: it measures the same services the hosted
environment runs, started locally. Numbers that only a deployed environment
can produce — object-store latency, autoscaling, cost — are not measured here
and are not published.
"""

from __future__ import annotations

import argparse
import itertools
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import bundles, scenarios, telemetry
from .client import Client
from .stack import Stack, wait_for

NAMESPACES = {"@control_plane": "Tracewake/ControlPlane", "@worker": "Tracewake/Worker"}


def _versions(repository: Path) -> dict[str, str]:
    def command(*args: str) -> str:
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()

    return {
        "tracewake": json.loads(
            subprocess.run(
                [sys.executable, "-c", "import json,importlib.metadata as m; print(json.dumps(m.version('tracewake')))"],
                capture_output=True, text=True, check=True, cwd=repository,
            ).stdout
        ),
        "python": platform.python_version(),
        "go": command("go", "version"),
        "postgres": command("psql", "--version"),
        "platform": f"{platform.system()} {platform.machine()}",
        "commit": command("git", "-C", str(repository), "rev-parse", "HEAD"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evidence/results"))
    parser.add_argument("--work", type=Path, default=None, help="Working directory for the disposable stack.")
    parser.add_argument("--load-jobs", type=int, default=24)
    parser.add_argument("--ingest-bundles", type=int, default=10)
    parser.add_argument("--soak-seconds", type=float, default=60.0)
    parser.add_argument("--soak-interval", type=float, default=2.0)
    parser.add_argument("--recovery-steps", type=int, default=300, help="Agent steps in the runs the recovery scenario interrupts.")
    parser.add_argument("--keep", action="store_true", help="Leave the stack running directory in place.")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Shorten the run and skip the scenarios that only wait. For checking the harness, not for publishing numbers.",
    )
    arguments = parser.parse_args()
    if arguments.quick:
        arguments.load_jobs = min(arguments.load_jobs, 6)
        arguments.ingest_bundles = min(arguments.ingest_bundles, 6)
        arguments.soak_seconds = min(arguments.soak_seconds, 8.0)
        arguments.recovery_steps = min(arguments.recovery_steps, 200)

    repository = Path(__file__).resolve().parent.parent
    work = arguments.work or (repository / ".tracewake" / "evidence")
    if work.exists() and not arguments.keep:
        shutil.rmtree(work)
    stack = Stack(root=work, repository=repository)
    measurements: dict[str, Any] = {
        "evidence_version": 1,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": _versions(repository),
        "scenarios": {},
    }
    results = measurements["scenarios"]

    try:
        stack.start_postgres()
        stack.build()
        stack.start_control_plane()
        stack.start_worker()
        client = Client(stack.public_url, stack.credentials["token"])

        good, bad = bundles.pair(work / "bundles")
        extra = bundles.series(work / "bundles", max(0, arguments.ingest_bundles - 2))
        results["ingestion"] = scenarios.ingestion(client, [good, bad, *extra])
        run_ids = results["ingestion"]["run_ids"]
        # A normalized job input is unique per workspace, so each analysis needs
        # its own pair of runs rather than repeating one request.
        pairs = iter(itertools.permutations(run_ids, 2))
        needed = arguments.load_jobs + int(arguments.soak_seconds / arguments.soak_interval) + 6
        available = len(run_ids) * (len(run_ids) - 1)
        if available < needed:
            raise SystemExit(f"--ingest-bundles {len(run_ids)} yields {available} run pairs; this run needs {needed}")

        results["analysis_load"] = scenarios.analysis_load(client, pairs, arguments.load_jobs, "load")
        results["soak"] = scenarios.soak(client, pairs, arguments.soak_seconds, arguments.soak_interval, "soak")

        otlp = client.analyze("otlp", [run_ids[0]], "agreement")
        otlp_job = scenarios.wait_for_job(client, otlp["job_id"])
        results["hosted_matches_local"] = scenarios.hosted_matches_local(client, otlp_job, good, work)

        original = results["analysis_load"]["first_request"]
        repeated = client.analyze("diff", original["run_ids"], original["idempotency_key"], "align-v1")
        results["idempotent_replay"] = {
            "original_job_id": results["analysis_load"]["job_ids"][0],
            "replayed_job_id": repeated["job_id"],
            "same_job": repeated["job_id"] == results["analysis_load"]["job_ids"][0],
        }

        large = bundles.pair(work / "bundles", steps=arguments.recovery_steps, prefix="large-")
        large_runs = scenarios.ingestion(client, list(large))["run_ids"]
        results["worker_recovery"] = scenarios.worker_recovery(stack, client, (large_runs[0], large_runs[1]), "recovery")
        results["result_provenance"] = scenarios.result_provenance(client, results["worker_recovery"]["job_id"])

        stack.stop_worker()
        results["late_completion"] = scenarios.late_completion(stack, client, next(pairs), "late-completion")
        results["retry_exhaustion"] = scenarios.retry_exhaustion(stack, client, next(pairs), "retry-exhaustion")
        results["artifact_mismatch"] = scenarios.artifact_mismatch(stack, client, run_ids[1], "artifact-mismatch")
        if not arguments.quick:
            results["outbox_backlog"] = scenarios.outbox_backlog(stack, client, run_ids[0], "outbox-backlog", 150)
        stack.start_worker()
        first, second = next(pairs)
        drained = client.analyze("diff", [first, second], "after-faults", "align-v1")
        results["service_resumes"] = {"state": scenarios.wait_for_job(client, drained["job_id"], 240)["state"]}

        results["reconciler_failure"] = scenarios.reconciler_failure(stack, 20)

        workspace = subprocess.run(
            [str(stack.root / "tracewaked"), "bootstrap"],
            env=stack._environment(), capture_output=True, text=True, check=True,
        ).stdout
        other_token = dict(line.split("=", 1) for line in workspace.strip().splitlines())["token"]
        results["tenant_isolation"] = scenarios.tenant_isolation(
            stack, client, other_token, run_ids[0], results["analysis_load"]["job_ids"][0]
        )

        results["backup_and_restore"] = scenarios.backup_and_restore(
            stack, client, run_ids[0], results["analysis_load"]["job_ids"][0]
        )
        results["migration"] = scenarios.migration(stack)
        results["local_independence"] = scenarios.local_independence(repository, work)

        stack.start_worker()
        wait_for(lambda: stack.worker is not None and stack.worker.poll() is None, 10, "the worker to restart")
        time.sleep(6)
    finally:
        stack.stop()

    records = telemetry.read([stack.telemetry_dir / "control-plane.jsonl", stack.telemetry_dir / "worker.jsonl"])
    collected = telemetry.samples(records)
    measurements["telemetry"] = summarise(records, collected)
    evaluations = [
        telemetry.evaluate(alarm, collected, NAMESPACES) for alarm in telemetry.load_alarms(repository / telemetry.ALARMS)
    ]
    measurements["alarms"] = {
        "evaluations": evaluations,
        "raised": sorted(item["alarm"] for item in evaluations if item["state"] == "ALARM"),
        "tested_conditions_that_moved_a_metric": sorted(
            item["tested_condition"] for item in evaluations if item["tested_condition"] and item["responded"]
        ),
        "tested_conditions_without_a_signal": sorted(
            item["tested_condition"] for item in evaluations if item["tested_condition"] and not item["responded"]
        ),
    }
    measurements["latency"] = {
        name: telemetry.percentiles(results[name][field])
        for name, field in (
            ("ingestion", "upload_to_ready_ms"),
            ("analysis_load", "create_to_terminal_ms"),
            ("soak", "create_to_terminal_ms"),
        )
    }
    measurements["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "measurements.json").write_text(
        json.dumps(measurements, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name in ("control-plane.jsonl", "worker.jsonl"):
        source = stack.telemetry_dir / name
        if source.exists():
            shutil.copyfile(source, arguments.output / name)
    print(f"wrote {arguments.output / 'measurements.json'}")
    return 0


def summarise(records: list[dict[str, Any]], collected: list[telemetry.Sample]) -> dict[str, Any]:
    spans = telemetry.spans(records)
    traces: dict[str, set[str]] = {}
    for span in spans:
        traces.setdefault(span["trace_id"], set()).add(span["service_name"])
    crossing = [trace for trace, services in traces.items() if len(services) > 1]
    by_metric: dict[str, int] = {}
    series: set[tuple[str, str, str]] = set()
    for sample in collected:
        by_metric[sample.metric] = by_metric.get(sample.metric, 0) + 1
        series.add((sample.namespace, sample.metric, json.dumps(sample.dimensions, sort_keys=True)))
    return {
        "span_count": len(spans),
        "span_names": sorted({span["name"] for span in spans}),
        "trace_count": len(traces),
        "traces_crossing_services": len(crossing),
        "largest_trace_spans": max((len([s for s in spans if s["trace_id"] == t]) for t in traces), default=0),
        "metric_records": len(collected),
        "metric_series": len(series),
        "samples_by_metric": dict(sorted(by_metric.items())),
    }


if __name__ == "__main__":
    raise SystemExit(main())
