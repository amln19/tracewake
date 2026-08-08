from __future__ import annotations

import argparse
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

CONTRACT_SCHEMA_VERSION = 1
WORKER_PROTOCOL_VERSION = 1

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TraceParent = Annotated[str, StringConstraints(pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")]
ObjectKey = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedMessage = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureCode(StrEnum):
    INVALID_BUNDLE = "invalid_bundle"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_RESULT = "invalid_result"
    UNAUTHORIZED_INPUT = "unauthorized_input"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    ARTIFACT_COMMIT_FAILED = "artifact_commit_failed"
    TRANSIENT_DEPENDENCY = "transient_dependency"
    INTERNAL = "internal"
    RETRY_EXHAUSTED = "retry_exhausted"


class Failure(ContractModel):
    schema_version: Literal[CONTRACT_SCHEMA_VERSION]
    code: FailureCode
    message: BoundedMessage
    retryable: bool


class ArtifactRef(ContractModel):
    artifact_id: UUID
    object_key: ObjectKey
    object_version: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    digest: Digest
    size: int = Field(ge=0)
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    schema_name: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    schema_version: int | None = Field(default=None, ge=1)


class RunProvenance(ContractModel):
    run_id: UUID
    logical_run_digest: Digest
    bundle_digest: Digest
    bundle_object_key: ObjectKey
    bundle_object_version: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    event_schema_version: int = Field(ge=1)
    cassette_format_version: int = Field(ge=1)
    bundle_format_version: int = Field(ge=1)


class ResultProvenance(ContractModel):
    inputs: list[RunProvenance] = Field(min_length=1, max_length=2)
    analysis_profile: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    tracewake_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    worker_build: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    produced_at: AwareDatetime


class ValidationResult(ContractModel):
    kind: Literal["validation"] = "validation"
    schema_version: Literal[CONTRACT_SCHEMA_VERSION]
    valid: Literal[True]
    run_id: UUID
    event_count: int = Field(ge=0)
    logical_run_digest: Digest
    bundle_digest: Digest
    provenance: ResultProvenance


class AlignmentColumn(ContractModel):
    good_index: int | None = Field(default=None, ge=0)
    bad_index: int | None = Field(default=None, ge=0)
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _has_a_side(self) -> AlignmentColumn:
        if self.good_index is None and self.bad_index is None:
            raise ValueError("an alignment column must contain at least one side")
        return self


class DiffResult(ContractModel):
    kind: Literal["diff"] = "diff"
    schema_version: Literal[CONTRACT_SCHEMA_VERSION]
    profile: Literal["lexical-v1"]
    score: float
    divergence: int | None = Field(default=None, ge=1)
    good_step_count: int = Field(ge=0)
    bad_step_count: int = Field(ge=1)
    alignment: list[AlignmentColumn]
    provenance: ResultProvenance
    html: ArtifactRef


class OtlpResult(ContractModel):
    kind: Literal["otlp"] = "otlp"
    schema_version: Literal[CONTRACT_SCHEMA_VERSION]
    span_count: int = Field(ge=1)
    provenance: ResultProvenance
    artifact: ArtifactRef


class PprofResult(ContractModel):
    kind: Literal["pprof"] = "pprof"
    schema_version: Literal[CONTRACT_SCHEMA_VERSION]
    sample_count: int = Field(ge=0)
    provenance: ResultProvenance
    artifact: ArtifactRef


SemanticResult = Annotated[
    ValidationResult | DiffResult | OtlpResult | PprofResult,
    Field(discriminator="kind"),
]


class ResultEnvelope(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    status: Literal["succeeded", "failed"]
    result: SemanticResult | None = None
    failure: Failure | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> ResultEnvelope:
        if self.status == "succeeded" and (self.result is None or self.failure is not None):
            raise ValueError("a succeeded envelope requires only result")
        if self.status == "failed" and (self.failure is None or self.result is not None):
            raise ValueError("a failed envelope requires only failure")
        return self


class AnalysisProfile(ContractModel):
    name: Literal["lexical-v1"]
    version: Literal[1]
    token_pattern: Literal[r"[A-Za-z0-9_./-]+"]
    case: Literal["lower"]
    blank_token: Literal["."]
    weights: dict[Literal["tool", "args", "reasoning", "files"], float]
    argument_weights: dict[Literal["target", "rest"], float]
    line_falloff: Literal[50.0]
    gap_open: Literal[-1.0]
    gap_extend: Literal[-0.2]
    score_transform: Literal["2*s-1"]
    divergence_rule: Literal["last-target-agreement"]


class JobNotification(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    job_id: UUID
    job_version: int = Field(ge=1)
    operation: Literal["validate", "diff", "otlp", "pprof"]
    # Carrying the W3C trace context in the notification is what lets one trace
    # span the transition the queue makes asynchronous.
    traceparent: TraceParent | None = None


class ClaimRequest(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    notification: JobNotification
    worker_id: UUID


class Claim(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    job_id: UUID
    attempt_number: int = Field(ge=1, le=3)
    attempt_token: Annotated[str, StringConstraints(min_length=43, max_length=256)]
    lease_expires_at: AwareDatetime
    input_artifacts: list[ArtifactRef] = Field(min_length=1, max_length=2)
    operation: Literal["validate", "diff", "otlp", "pprof"]
    profile: Literal["lexical-v1"] | None = None


class Heartbeat(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    attempt_number: int = Field(ge=1, le=3)
    observed_lease_expires_at: AwareDatetime


class Progress(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    attempt_number: int = Field(ge=1, le=3)
    sequence: int = Field(ge=1)
    stage: Literal[
        "claiming",
        "downloading",
        "validating",
        "analyzing",
        "uploading",
        "committing",
    ]
    message: BoundedMessage


class ArtifactCommit(ContractModel):
    protocol_version: Literal[WORKER_PROTOCOL_VERSION]
    attempt_number: int = Field(ge=1, le=3)
    artifact: ArtifactRef
    result: SemanticResult


class PublicJobRequest(ContractModel):
    operation: Literal["diff", "otlp", "pprof"]
    run_ids: list[UUID] = Field(min_length=1, max_length=2)
    profile: Literal["lexical-v1"] | None = None

    @model_validator(mode="after")
    def _operation_shape(self) -> PublicJobRequest:
        expected = 2 if self.operation == "diff" else 1
        if len(self.run_ids) != expected:
            raise ValueError(f"{self.operation} requires {expected} run id(s)")
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("run ids must be distinct")
        if self.operation == "diff" and self.profile != "lexical-v1":
            raise ValueError("diff requires profile lexical-v1")
        if self.operation != "diff" and self.profile is not None:
            raise ValueError("only diff accepts a profile")
        return self


class UploadDeclaration(ContractModel):
    bundle_format_version: Literal[1]
    bundle_digest: Digest
    bundle_size: int = Field(ge=0, le=256 * 1024 * 1024)


class UploadGrant(ContractModel):
    upload_id: UUID
    run_id: UUID
    required_digest: Digest
    required_size: int = Field(ge=0, le=256 * 1024 * 1024)
    upload_url: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    # Object stores bind checksum and content-type headers into the signature,
    # so the client must send exactly what the grant names.
    upload_headers: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=64)],
        Annotated[str, StringConstraints(min_length=1, max_length=256)],
    ] = Field(default_factory=dict, max_length=8)
    expires_at: AwareDatetime


class UploadCompletion(ContractModel):
    object_version: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    digest: Digest
    size: int = Field(ge=0, le=256 * 1024 * 1024)


class PublicArtifact(ContractModel):
    artifact_id: UUID
    kind: Literal[
        "diff_json",
        "diff_html",
        "otlp_result_json",
        "otlp_json",
        "pprof_result_json",
        "pprof",
    ]
    digest: Digest
    size: int = Field(ge=0)
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    schema_name: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    schema_version: int | None = Field(default=None, ge=1)
    retention_expires_at: AwareDatetime


class RunView(ContractModel):
    run_id: UUID
    state: Literal["pending", "uploaded", "validating", "ready", "invalid", "deleted"]
    bundle_format_version: int = Field(ge=1)
    bundle_digest: Digest
    logical_run_digest: Digest | None = None
    cassette_format_version: int | None = Field(default=None, ge=1)
    event_schema_version: int | None = Field(default=None, ge=1)
    event_count: int | None = Field(default=None, ge=0, le=100_000)
    failure: Failure | None = None
    created_at: AwareDatetime
    ready_at: AwareDatetime | None = None
    retention_expires_at: AwareDatetime

    @model_validator(mode="after")
    def _ready_shape(self) -> RunView:
        validated = (
            self.logical_run_digest,
            self.cassette_format_version,
            self.event_schema_version,
            self.event_count,
            self.ready_at,
        )
        if self.state == "ready" and (any(value is None for value in validated) or self.failure):
            raise ValueError("a ready run requires validated identity and no failure")
        if self.state == "invalid" and self.failure is None:
            raise ValueError("an invalid run requires a bounded failure")
        return self


class AttemptView(ContractModel):
    attempt_number: int = Field(ge=1, le=3)
    state: Literal["running", "succeeded", "failed", "fenced", "cancelled"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    failure: Failure | None = None


class JobView(ContractModel):
    job_id: UUID
    operation: Literal["diff", "otlp", "pprof"]
    run_ids: list[UUID] = Field(min_length=1, max_length=2)
    profile: Literal["lexical-v1"] | None = None
    state: Literal["queued", "running", "retry_wait", "succeeded", "failed", "cancelled"]
    current_attempt_number: int | None = Field(default=None, ge=1, le=3)
    attempts: list[AttemptView] = Field(max_length=3)
    progress: Progress | None = None
    cancel_requested_at: AwareDatetime | None = None
    failure: Failure | None = None
    artifacts: list[PublicArtifact]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    terminal_at: AwareDatetime | None = None


class PublicErrorDetail(ContractModel):
    code: Literal[
        "invalid_request",
        "unauthenticated",
        "forbidden",
        "not_found",
        "conflict",
        "idempotency_conflict",
        "unsupported_version",
        "rate_limited",
        "internal",
    ]
    message: BoundedMessage
    request_id: UUID


class PublicError(ContractModel):
    error: PublicErrorDetail


_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "analysis-profile": AnalysisProfile,
    "artifact-commit": ArtifactCommit,
    "claim": Claim,
    "claim-request": ClaimRequest,
    "failure": Failure,
    "heartbeat": Heartbeat,
    "job-notification": JobNotification,
    "progress": Progress,
    "public-error": PublicError,
    "public-job": JobView,
    "public-job-request": PublicJobRequest,
    "public-run": RunView,
    "result-envelope": ResultEnvelope,
    "upload-completion": UploadCompletion,
    "upload-declaration": UploadDeclaration,
    "upload-grant": UploadGrant,
}


def schema_documents() -> dict[str, dict[str, object]]:
    return {
        f"{name}.schema.json": model.model_json_schema(mode="serialization")
        for name, model in sorted(_SCHEMA_MODELS.items())
    }


def write_schema_documents(root: Path, *, check: bool = False) -> None:
    expected = {
        name: json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        for name, schema in schema_documents().items()
    }
    if check:
        actual = {
            path.name: path.read_text(encoding="utf-8") for path in root.glob("*.json")
        }
        if actual != expected:
            raise SystemExit(f"generated contract schemas differ from committed files in {root}")
        return
    root.mkdir(parents=True, exist_ok=True)
    for path in root.glob("*.json"):
        if path.name not in expected:
            path.unlink()
    for name, text in expected.items():
        (root / name).write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Tracewake contract JSON Schemas")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    write_schema_documents(args.output, check=args.check)


if __name__ == "__main__":
    main()
