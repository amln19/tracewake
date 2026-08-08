"""The retained operational run, checked as an artifact.

`evidence/README.md` explains how to reproduce it. These tests keep the
retained results honest: that the telemetry carries nothing sensitive, that
every claim the documentation makes about the run is present in the run, and
that each alarm condition the deployment names actually moved a metric.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

RESULTS = Path("evidence/results")

pytestmark = pytest.mark.skipif(
    not (RESULTS / "measurements.json").is_file(),
    reason="the retained operational run is not part of the distribution",
)


@pytest.fixture(scope="module")
def measurements() -> dict[str, Any]:
    return dict(json.loads((RESULTS / "measurements.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def telemetry() -> str:
    return "\n".join(
        (RESULTS / name).read_text(encoding="utf-8") for name in ("control-plane.jsonl", "worker.jsonl")
    )


FORBIDDEN = {
    "a tenant token": "locus_",
    "a worker credential": "worker_",
    "an attempt token": "attempt_",
    "an authorization header": "Bearer",
    "a signed object URL": "signature=",
    "an object store request signature": "X-Amz",
    "an object key": "workspaces/",
    "a bundle path": "bundle.tar",
    "a server secret": "pepper",
}


@pytest.mark.parametrize("description,needle", sorted(FORBIDDEN.items()))
def test_telemetry_carries_nothing_sensitive(telemetry: str, description: str, needle: str) -> None:
    assert needle not in telemetry, f"the telemetry stream contains {description}"


def test_telemetry_carries_no_content_digests(telemetry: str) -> None:
    # Digests identify tenant bytes. Identifiers the server generated are fine;
    # a digest in an operational stream is a fingerprint of uploaded content.
    assert not re.search(r"[0-9a-f]{64}", telemetry)


def test_one_trace_covers_both_languages(measurements: dict[str, Any]) -> None:
    summary = measurements["telemetry"]
    assert summary["traces_crossing_services"] > 0
    assert {"job.claim", "artifact.commit", "reconcile"} <= set(summary["span_names"])
    assert {"worker.execute", "worker.download", "worker.upload", "worker.commit"} <= set(summary["span_names"])


def test_the_metric_stream_stays_bounded(measurements: dict[str, Any]) -> None:
    assert measurements["telemetry"]["metric_series"] <= 1024


def test_every_tested_alarm_condition_moved_a_metric(measurements: dict[str, Any]) -> None:
    alarms = measurements["alarms"]
    assert alarms["tested_conditions_without_a_signal"] == []
    assert alarms["tested_conditions_that_moved_a_metric"]


def test_a_killed_worker_produced_one_authoritative_result(measurements: dict[str, Any]) -> None:
    recovery = measurements["scenarios"]["worker_recovery"]
    assert recovery["state"] == "succeeded"
    assert recovery["fence_reason"] == "lease_lost"
    assert recovery["succeeded_attempts"] == 1
    assert [attempt["state"] for attempt in recovery["attempts"]] == ["fenced", "succeeded"]


def test_a_stale_attempt_could_not_report_or_commit(measurements: dict[str, Any]) -> None:
    requests = measurements["scenarios"]["late_completion"]["stale_attempt_requests"]
    assert set(requests) == {"heartbeat", "progress", "complete"}
    assert all(outcome == "LeaseLost" for outcome in requests.values()), requests


def test_exhausted_retries_end_terminally_with_no_artifacts(measurements: dict[str, Any]) -> None:
    exhaustion = measurements["scenarios"]["retry_exhaustion"]
    assert exhaustion["attempt_states"] == ["retry_wait", "retry_wait", "failed"]
    assert exhaustion["state"] == "failed"
    assert exhaustion["failure_code"] == "retry_exhausted"
    assert exhaustion["artifacts"] == 0


def test_neither_boundary_accepted_a_contradicted_artifact(measurements: dict[str, Any]) -> None:
    mismatch = measurements["scenarios"]["artifact_mismatch"]
    assert mismatch["contradicting_upload"] != "accepted"
    assert mismatch["contradicting_commit"] != "accepted"
    assert mismatch["artifacts"] == 0


def test_faults_left_the_service_working(measurements: dict[str, Any]) -> None:
    assert measurements["scenarios"]["service_resumes"]["state"] == "succeeded"


def test_hosted_analysis_agreed_with_local_locus(measurements: dict[str, Any]) -> None:
    agreement = measurements["scenarios"]["hosted_matches_local"]
    assert agreement["identical_bytes"]
    assert agreement["hosted_span_count"] == agreement["local_span_count"]


def test_an_idempotent_request_returned_its_original_job(measurements: dict[str, Any]) -> None:
    assert measurements["scenarios"]["idempotent_replay"]["same_job"]


def test_a_second_workspace_saw_nothing(measurements: dict[str, Any]) -> None:
    isolation = measurements["scenarios"]["tenant_isolation"]
    assert isolation["runs_visible"] == 0
    assert isolation["audit_visible"] == 0
    assert isolation["run"] == "HTTP 404"
    assert isolation["job"] == "HTTP 404"
    assert isolation["unknown_token"] == "HTTP 401"


def test_authoritative_state_survived_a_restore(measurements: dict[str, Any]) -> None:
    restore = measurements["scenarios"]["backup_and_restore"]
    assert restore["before"] == restore["after"]
    assert restore["run_still_ready"] and restore["job_still_terminal"]


def test_migrations_are_ordered_and_idempotent(measurements: dict[str, Any]) -> None:
    migration = measurements["scenarios"]["migration"]
    assert migration["applied_versions"] == sorted(migration["applied_versions"])
    assert not migration["reapplied"]
    assert migration["ledger_rows"] == len(migration["applied_versions"])


def test_local_locus_needed_no_hosted_service(measurements: dict[str, Any]) -> None:
    assert measurements["scenarios"]["local_independence"]["identical"]


def test_every_analysis_under_load_succeeded(measurements: dict[str, Any]) -> None:
    assert measurements["scenarios"]["analysis_load"]["outcomes"] == {
        "succeeded": measurements["scenarios"]["analysis_load"]["jobs"]
    }
    assert measurements["scenarios"]["soak"]["outcomes"] == {"succeeded": measurements["scenarios"]["soak"]["jobs"]}


def test_every_published_number_comes_from_this_run(measurements: dict[str, Any]) -> None:
    """The README's measured-behaviour table may only restate the retained run."""
    latency = measurements["latency"]
    scenarios = measurements["scenarios"]
    telemetry_summary = measurements["telemetry"]
    recovery = scenarios["worker_recovery"]
    soak = scenarios["soak"]
    published = [
        f"p50 {round(latency['ingestion']['p50_ms'])} ms, p95 {round(latency['ingestion']['p95_ms'])} ms",
        f"drained in {round(scenarios['analysis_load']['drain_seconds'], 2)} s",
        f"p50 {round(latency['analysis_load']['p50_ms'])} ms, p95 {round(latency['analysis_load']['p95_ms'])} ms",
        f"{soak['jobs']} of {soak['jobs']} succeeded, p50 {round(latency['soak']['p50_ms'])} ms",
        f"{round(recovery['kill_to_fence_seconds'], 1)} s",
        f"{round(recovery['kill_to_success_seconds'], 1)} s",
        f"{telemetry_summary['span_count']} across {telemetry_summary['trace_count']} traces",
        f"{telemetry_summary['traces_crossing_services']}, up to {telemetry_summary['largest_trace_spans']} spans each",
        f"{telemetry_summary['metric_series']}",
        f"{round(soak['first_half_mean_ms'])} ms across its first half",
        f"{round(soak['second_half_mean_ms'])} ms across its second",
        f"{len(scenarios['ingestion']['run_ids'])} bundles",
        f"{scenarios['analysis_load']['jobs']} diff analyses",
    ]
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("### Measured behaviour", 1)[-1].split("## Persistent formats", 1)[0]
    for value in published:
        assert value.lower() in section.lower(), f"the README does not restate {value!r} from the retained run"


def test_the_demonstration_recorded_every_step(measurements: dict[str, Any]) -> None:
    """Each step the README maps must be present and satisfying in the run."""
    scenarios = measurements["scenarios"]
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("### The demonstration", 1)[-1].split("## Persistent formats", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    for reference in re.findall(r"`([a-z_]+(?:\.[a-z_]+)?)`", "\n".join(rows)):
        name, _, field = reference.partition(".")
        assert name in scenarios, f"the README maps a step to {name}, which the run does not record"
        if field:
            assert scenarios[name][field], f"{reference} recorded nothing"

    progress = scenarios["worker_recovery"]["progress_while_running"]
    assert progress["attempt_number"] == 1 and progress["stage"]
    provenance = scenarios["result_provenance"]
    assert provenance["artifact_kinds"] == ["diff_html", "diff_json"]
    assert provenance["downloads_match_recorded_identity"]
    assert provenance["html_is_self_contained"]
    assert provenance["analysis_profile"] == "lexical-v1"
    assert provenance["result_schema"] == {"name": "result-envelope", "version": 1}
    assert len(provenance["input_digests"]) == 2
    for digests in provenance["input_digests"]:
        assert all(digests.values())
    assert {"job.created", "attempt.claimed", "job.succeeded"} <= set(provenance["audit_events"])


AWS_RESULTS = RESULTS / "aws" / "measurements.json"

deployed_only = pytest.mark.skipif(
    not AWS_RESULTS.is_file(), reason="no deployed run is retained"
)


@pytest.fixture(scope="module")
def deployed() -> dict[str, Any]:
    return dict(json.loads(AWS_RESULTS.read_text(encoding="utf-8")))


@deployed_only
def test_alarms_transitioned_on_the_deployment(deployed: dict[str, Any]) -> None:
    """The gate needs CloudWatch itself to have fired, not a local evaluation."""
    fired = {
        item["alarm"]
        for item in deployed["alarm_history"]
        if item["summary"].endswith("from OK to ALARM")
    }
    assert {"attempts-losing-their-lease", "reconciler-failing"} <= fired, fired
    assert len(deployed["alarms"]) == 14


@deployed_only
def test_a_partitioned_attempt_was_fenced_and_replaced(deployed: dict[str, Any]) -> None:
    for job in deployed["partition"]["recovery"]:
        assert job["state"] == "succeeded"
        assert [(a["n"], a["state"], a["failure"]) for a in job["attempts"]] == [
            (1, "fenced", "lease_lost"),
            (2, "succeeded", None),
        ]
        assert sorted(job["artifacts"]) == ["diff_html", "diff_json"]


@deployed_only
def test_the_deployed_baseline_verified_every_artifact(deployed: dict[str, Any]) -> None:
    baseline = deployed["baseline"]
    assert baseline["job_state"] == "succeeded"
    assert baseline["idempotent"]
    assert all(item["digest_matches"] for item in baseline["artifacts"])
    assert all(run["state"] == "ready" for run in baseline["ingestion"])


@deployed_only
def test_no_cost_number_is_published(deployed: dict[str, Any]) -> None:
    # Cost Explorer had not ingested the window. Nothing may claim otherwise.
    assert "cost" not in json.dumps(deployed["metrics"]).lower()
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("### On a deployed environment", 1)[-1].split("### The demonstration", 1)[0]
    assert "cost remain unmeasured" in section or "cost remains unmeasured" in section


@deployed_only
def test_deployed_numbers_come_from_the_deployed_run(deployed: dict[str, Any]) -> None:
    metrics = deployed["metrics"]
    restore = deployed["point_in_time_restore"]
    minutes, seconds = divmod(restore["elapsed_seconds"], 60)
    published = [
        f"{round(metrics['queue_latency_ms_diff']['Minimum'])}–{round(metrics['queue_latency_ms_diff']['Maximum'])} ms",
        f"{round(metrics['queue_latency_ms_validate']['Minimum'])}–{round(metrics['queue_latency_ms_validate']['Maximum'])} ms",
        f"{round(metrics['job_duration_ms_diff_succeeded']['Minimum'])} ms",
        f"{minutes} min {seconds} s",
    ]
    # The fenced-attempt metric and the job records have to agree with each other.
    fenced = sum(
        1
        for job in deployed["partition"]["recovery"]
        for attempt in job["attempts"]
        if attempt["failure"] == "lease_lost"
    )
    assert fenced == int(metrics["attempts_fenced_lease_expired"]["Sum"])
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("### On a deployed environment", 1)[-1].split("### The demonstration", 1)[0]
    for value in published:
        assert value in section, f"the README does not restate {value!r} from the deployed run"
