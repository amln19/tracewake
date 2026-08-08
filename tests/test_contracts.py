from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracewake.contracts import (
    CONTRACT_SCHEMA_VERSION,
    Failure,
    FailureCode,
    Progress,
    ResultEnvelope,
    ValidationResult,
    schema_documents,
)


SCHEMA_ROOT = Path(__file__).parents[1] / "contracts" / "schemas" / "v1"


def test_contract_schemas_are_versioned_strict_and_bounded() -> None:
    assert CONTRACT_SCHEMA_VERSION == 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        Failure(
            schema_version=1,
            code=FailureCode.INVALID_BUNDLE,
            message="invalid",
            retryable=False,
            secret="must not be accepted",
        )
    with pytest.raises(ValidationError, match="at most 512"):
        Failure(
            schema_version=1,
            code=FailureCode.INVALID_BUNDLE,
            message="x" * 513,
            retryable=False,
        )


def test_failure_codes_are_a_closed_versioned_set() -> None:
    expected = {
        "invalid_bundle",
        "unsupported_version",
        "invalid_result",
        "unauthorized_input",
        "cancelled",
        "lease_lost",
        "artifact_commit_failed",
        "transient_dependency",
        "internal",
        "retry_exhausted",
    }
    assert {code.value for code in FailureCode} == expected


def test_result_envelope_discriminates_success_and_failure() -> None:
    success = ResultEnvelope.model_validate(
        {
            "protocol_version": 1,
            "status": "succeeded",
            "result": {
                "kind": "validation",
                "schema_version": 1,
                "valid": True,
                "run_id": "018f7f28-df62-7bc4-9f45-6e6c32a19484",
                "event_count": 0,
                "logical_run_digest": "0" * 64,
                "bundle_digest": "1" * 64,
                "provenance": {
                    "inputs": [
                        {
                            "run_id": "018f7f28-df62-7bc4-9f45-6e6c32a19484",
                            "logical_run_digest": "0" * 64,
                            "bundle_digest": "1" * 64,
                            "bundle_object_key": "workspaces/w/runs/r/bundle",
                            "bundle_object_version": "v1",
                            "event_schema_version": 3,
                            "cassette_format_version": 1,
                            "bundle_format_version": 1,
                        }
                    ],
                    "analysis_profile": "bundle-validation-v1",
                    "tracewake_version": "0.2.0",
                    "worker_build": "test-build",
                    "produced_at": "2026-08-06T12:00:00Z",
                },
            },
        }
    )
    assert isinstance(success.result, ValidationResult)

    failed = ResultEnvelope.model_validate(
        {
            "protocol_version": 1,
            "status": "failed",
            "failure": {
                "schema_version": 1,
                "code": "invalid_bundle",
                "message": "manifest mismatch",
                "retryable": False,
            },
        }
    )
    assert failed.failure is not None


def test_progress_rejects_unbounded_or_secret_payloads() -> None:
    with pytest.raises(ValidationError):
        Progress(
            protocol_version=1,
            attempt_number=1,
            sequence=1,
            stage="validating",
            message="x" * 513,
        )


def test_committed_json_schemas_match_pydantic_generation() -> None:
    generated = schema_documents()
    committed = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_ROOT.glob("*.json")
    }
    assert committed == generated
