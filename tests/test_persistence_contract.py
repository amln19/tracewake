from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
UP = (ROOT / "contracts/postgres/0001_hosted_contracts.up.sql").read_text(
    encoding="utf-8"
)
DOWN = (ROOT / "contracts/postgres/0001_hosted_contracts.down.sql").read_text(
    encoding="utf-8"
)


def test_initial_migration_contains_every_authoritative_table() -> None:
    expected = {
        "workspaces",
        "api_tokens",
        "worker_credentials",
        "runs",
        "job_inputs",
        "jobs",
        "job_attempts",
        "artifacts",
        "progress_snapshots",
        "idempotency_records",
        "outbox",
        "audit_records",
    }
    for table in expected:
        assert f"CREATE TABLE {table} (" in UP
        assert f"DROP TABLE {table};" in DOWN


def test_migration_encodes_workspace_attempt_and_object_identity_constraints() -> None:
    assert "FOREIGN KEY (run_a_id, workspace_id)" in UP
    assert "FOREIGN KEY (run_b_id, workspace_id)" in UP
    assert "FOREIGN KEY (id, current_attempt_number)" in UP
    assert "PRIMARY KEY (job_id, attempt_number)" in UP
    assert "UNIQUE (object_key, object_version)" in UP
    assert "max_attempts smallint NOT NULL DEFAULT 3 CHECK (max_attempts = 3)" in UP


def test_result_json_is_not_duplicated_in_postgres() -> None:
    assert "result jsonb" not in UP.lower()
    assert "result_payload" not in UP.lower()
    assert "result_artifact_id uuid" in UP
    assert "result_digest char(64)" in UP
    assert "result_schema_version integer" in UP


def test_outbox_and_audit_payloads_are_bounded() -> None:
    assert UP.count("octet_length(payload::text) <= 4096") == 2
    assert "UNIQUE (aggregate_type, aggregate_id, aggregate_version, topic)" in UP
    assert "interval '365 days'" in UP


def test_retention_and_lifecycle_constants_are_persistent() -> None:
    assert "interval '90 days'" in UP
    assert UP.count("interval '24 hours'") >= 2
    assert "declared_bundle_size BETWEEN 0 AND 268435456" in UP
    assert "event_count BETWEEN 0 AND 100000" in UP
