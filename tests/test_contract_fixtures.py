from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from contracttest.generate_fixtures import fixture_bytes
from locus.bundle import validate_bundle
from locus.contracts import (
    Claim,
    ClaimRequest,
    Failure,
    JobNotification,
    Progress,
    PublicJobRequest,
    ResultEnvelope,
    UploadDeclaration,
)
from locus.events import sha256_hex


ROOT = Path(__file__).parents[1] / "contracttest" / "fixtures" / "v1"
MODELS = {
    "failure": Failure,
    "job-notification": JobNotification,
    "claim-request": ClaimRequest,
    "claim": Claim,
    "progress": Progress,
    "public-job-request": PublicJobRequest,
    "result-envelope": ResultEnvelope,
    "upload-declaration": UploadDeclaration,
}


def _validate(path: Path, validator: str) -> str | None:
    if validator == "bundle":
        try:
            validate_bundle(path)
        except ValueError:
            return "invalid_archive"
        return None
    raw = path.read_bytes()
    try:
        MODELS[validator].model_validate_json(raw)
    except ValidationError as exc:
        data = json.loads(raw)
        if validator == "progress" and data.get("protocol_version") != 1:
            return "unsupported_version"
        if validator == "upload-declaration" and "bundle_digest" in str(exc):
            return "invalid_digest"
        if validator == "public-job-request":
            return "invalid_request"
        return "invalid_message"
    return None


def test_committed_fixtures_are_reproducible() -> None:
    expected, _ = fixture_bytes()
    actual = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in ROOT.rglob("*")
        if path.is_file()
    }
    assert actual == expected


def test_python_agrees_with_every_shared_fixture() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for fixture in manifest["fixtures"]:
        path = ROOT / fixture["path"]
        assert sha256_hex(path.read_bytes()) == fixture["sha256"]
        error = _validate(path, fixture["validator"])
        assert (error is None) is fixture["accepted"], fixture
        assert error == fixture["error_code"], fixture
