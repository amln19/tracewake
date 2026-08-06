from __future__ import annotations

import argparse
import gzip
import json
import shutil
import tempfile
from pathlib import Path

from locus.bundle import build_bundle
from locus.cassette import CassetteHeader, _line
from locus.contracts import (
    ArtifactRef,
    Claim,
    ClaimRequest,
    Failure,
    FailureCode,
    JobNotification,
    Progress,
    PublicJobRequest,
    ResultEnvelope,
    ResultProvenance,
    RunProvenance,
    UploadDeclaration,
    ValidationResult,
)
from locus.events import BlobRef, EventMeta, OutcomeEvent, StoredEvent, run_digest, sha256_hex

RUN_ID = "018f7f28-df62-7bc4-9f45-6e6c32a19484"
JOB_ID = "018f7f28-df62-7bc4-9f45-6e6c32a19485"
WORKER_ID = "018f7f28-df62-7bc4-9f45-6e6c32a19486"
ARTIFACT_ID = "018f7f28-df62-7bc4-9f45-6e6c32a19487"
SECOND_RUN_ID = "018f7f28-df62-7bc4-9f45-6e6c32a19488"
NOW = "2026-08-06T12:00:00Z"


def _json(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _provenance(bundle_digest: str, logical_digest: str) -> ResultProvenance:
    return ResultProvenance(
        inputs=[
            RunProvenance(
                run_id=RUN_ID,
                logical_run_digest=logical_digest,
                bundle_digest=bundle_digest,
                bundle_object_key=f"workspaces/w/runs/{RUN_ID}/bundle",
                bundle_object_version="version-1",
                event_schema_version=3,
                cassette_format_version=1,
                bundle_format_version=1,
            )
        ],
        analysis_profile="bundle-validation-v1",
        locus_version="0.2.0",
        worker_build="fixture-build",
        produced_at=NOW,
    )


def _fixed_bundle(root: Path) -> tuple[bytes, str, str]:
    cassette = root / "cassette"
    blob = b"fixed bundle blob\n"
    blob_digest = sha256_hex(blob)
    event = StoredEvent(
        run_id=RUN_ID,
        seq=0,
        event=OutcomeEvent(
            meta=EventMeta(recorded_at=1_700_000_000.0),
            status="ok",
            patch=BlobRef(digest=blob_digest, size=len(blob)),
        ),
    )
    logical = run_digest([event])
    header = CassetteHeader(
        locus_version="0.2.0",
        schema_version=3,
        run_id=RUN_ID,
        name="contract-fixture",
        recorded_at=1_700_000_000.0,
        finished_at=1_700_000_001.0,
        status="ok",
        models=[],
        event_count=1,
        digest=logical,
    )
    cassette.mkdir()
    (cassette / "cassette.jsonl").write_text(
        header.model_dump_json() + "\n" + _line(event) + "\n", encoding="utf-8"
    )
    blob_path = cassette / "blobs" / blob_digest[:2] / blob_digest[2:4] / blob_digest
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(blob)
    bundle = build_bundle(cassette, root / "bundle-v1.tar").read_bytes()
    return bundle, sha256_hex(bundle), logical


def fixture_bytes() -> tuple[dict[str, bytes], list[dict[str, object]]]:
    with tempfile.TemporaryDirectory() as temporary:
        bundle, bundle_digest, logical_digest = _fixed_bundle(Path(temporary))

    artifact = ArtifactRef(
        artifact_id=ARTIFACT_ID,
        object_key=f"workspaces/w/jobs/{JOB_ID}/attempts/1/result.json",
        object_version="version-1",
        digest="4" * 64,
        size=123,
        media_type="application/json",
        schema_name="validation-result",
        schema_version=1,
    )
    notification = JobNotification(
        protocol_version=1,
        job_id=JOB_ID,
        job_version=1,
        operation="validate",
    )
    accepted: dict[str, tuple[str, bytes]] = {
        "accepted/bundle-v1.tar": ("bundle", bundle),
        "accepted/failure.json": (
            "failure",
            _json(
                Failure(
                    schema_version=1,
                    code=FailureCode.INVALID_BUNDLE,
                    message="manifest digest mismatch",
                    retryable=False,
                )
            ),
        ),
        "accepted/failure-retry-exhausted.json": (
            "failure",
            _json(
                Failure(
                    schema_version=1,
                    code=FailureCode.RETRY_EXHAUSTED,
                    message="retry limit reached",
                    retryable=False,
                )
            ),
        ),
        "accepted/job-notification.json": ("job-notification", _json(notification)),
        "accepted/claim-request.json": (
            "claim-request",
            _json(
                ClaimRequest(
                    protocol_version=1,
                    notification=notification,
                    worker_id=WORKER_ID,
                )
            ),
        ),
        "accepted/claim.json": (
            "claim",
            _json(
                Claim(
                    protocol_version=1,
                    job_id=JOB_ID,
                    attempt_number=1,
                    attempt_token="a" * 43,
                    lease_expires_at="2026-08-06T12:01:00Z",
                    input_artifacts=[artifact],
                    operation="validate",
                )
            ),
        ),
        "accepted/progress.json": (
            "progress",
            _json(
                Progress(
                    protocol_version=1,
                    attempt_number=1,
                    sequence=1,
                    stage="validating",
                    message="validating bundle",
                )
            ),
        ),
        "accepted/public-job-request.json": (
            "public-job-request",
            _json(
                PublicJobRequest(
                    operation="diff",
                    run_ids=[RUN_ID, SECOND_RUN_ID],
                    profile="lexical-v1",
                )
            ),
        ),
        "accepted/result-envelope.json": (
            "result-envelope",
            _json(
                ResultEnvelope(
                    protocol_version=1,
                    status="succeeded",
                    result=ValidationResult(
                        schema_version=1,
                        valid=True,
                        run_id=RUN_ID,
                        event_count=1,
                        logical_run_digest=logical_digest,
                        bundle_digest=bundle_digest,
                        provenance=_provenance(bundle_digest, logical_digest),
                    ),
                )
            ),
        ),
        "accepted/upload-declaration.json": (
            "upload-declaration",
            _json(
                UploadDeclaration(
                    bundle_format_version=1,
                    bundle_digest=bundle_digest,
                    bundle_size=len(bundle),
                )
            ),
        ),
    }
    rejected: dict[str, tuple[str, str, bytes]] = {
        "rejected/compressed-bundle-v1.tar.gz": (
            "bundle",
            "invalid_archive",
            gzip.compress(bundle, mtime=0),
        ),
        "rejected/failure-extra-field.json": (
            "failure",
            "invalid_message",
            _json(
                {
                    "schema_version": 1,
                    "code": "invalid_bundle",
                    "message": "invalid",
                    "retryable": False,
                    "secret": "unexpected",
                }
            ),
        ),
        "rejected/progress-unknown-version.json": (
            "progress",
            "unsupported_version",
            _json(
                {
                    "protocol_version": 2,
                    "attempt_number": 1,
                    "sequence": 1,
                    "stage": "validating",
                    "message": "invalid version",
                }
            ),
        ),
        "rejected/public-job-duplicate-run.json": (
            "public-job-request",
            "invalid_request",
            _json(
                {
                    "operation": "diff",
                    "run_ids": [RUN_ID, RUN_ID],
                    "profile": "lexical-v1",
                }
            ),
        ),
        "rejected/result-conflicting-outcomes.json": (
            "result-envelope",
            "invalid_message",
            _json(
                {
                    "protocol_version": 1,
                    "status": "failed",
                    "result": {
                        "kind": "validation",
                        "schema_version": 1,
                        "valid": True,
                        "run_id": RUN_ID,
                        "event_count": 1,
                        "logical_run_digest": logical_digest,
                        "bundle_digest": bundle_digest,
                        "provenance": _provenance(
                            bundle_digest, logical_digest
                        ).model_dump(mode="json"),
                    },
                    "failure": {
                        "schema_version": 1,
                        "code": "invalid_bundle",
                        "message": "conflict",
                        "retryable": False,
                    },
                }
            ),
        ),
        "rejected/upload-invalid-digest.json": (
            "upload-declaration",
            "invalid_digest",
            _json(
                {
                    "bundle_format_version": 1,
                    "bundle_digest": "not-a-digest",
                    "bundle_size": len(bundle),
                }
            ),
        ),
    }
    files = {path: data for path, (_, data) in accepted.items()}
    files.update({path: data for path, (_, _, data) in rejected.items()})
    manifest: list[dict[str, object]] = []
    for path, (validator, data) in sorted(accepted.items()):
        manifest.append(
            {
                "path": path,
                "validator": validator,
                "accepted": True,
                "error_code": None,
                "sha256": sha256_hex(data),
            }
        )
    for path, (validator, error, data) in sorted(rejected.items()):
        manifest.append(
            {
                "path": path,
                "validator": validator,
                "accepted": False,
                "error_code": error,
                "sha256": sha256_hex(data),
            }
        )
    files["manifest.json"] = _json({"fixture_version": 1, "fixtures": manifest})
    return files, manifest


def write_fixtures(root: Path, *, check: bool) -> None:
    expected, _ = fixture_bytes()
    if check:
        actual = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        if actual != expected:
            raise SystemExit(f"generated fixtures differ from committed files in {root}")
        return
    if root.exists():
        shutil.rmtree(root)
    for relative, data in expected.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate shared Locus contract fixtures")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    write_fixtures(args.output, check=args.check)


if __name__ == "__main__":
    main()
