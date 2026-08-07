"""The deployed environment's exposure boundaries.

These are static checks on the Terraform configuration. They cannot prove what
a live account does, but they fail loudly if the settings that keep the
database, the artifact bucket, and the worker API off the public internet are
edited away.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOYMENT = Path("deploy/aws")

# Infrastructure is not packaged, so these checks only apply to a checkout.
pytestmark = pytest.mark.skipif(
    not DEPLOYMENT.is_dir(), reason="deployment configuration is not part of the distribution"
)


def read(name: str) -> str:
    return (DEPLOYMENT / name).read_text(encoding="utf-8")


def test_database_is_private_and_encrypted() -> None:
    database = read("database.tf")
    assert "publicly_accessible    = false" in database
    assert "storage_encrypted     = true" in database
    assert "db_subnet_group_name   = aws_db_subnet_group.main.name" in database
    assert "subnet_ids = aws_subnet.private[*].id" in database


def test_artifact_bucket_blocks_public_access_and_plain_http() -> None:
    storage = read("storage.tf")
    for setting in (
        "block_public_acls       = true",
        "block_public_policy     = true",
        "ignore_public_acls      = true",
        "restrict_public_buckets = true",
    ):
        assert setting in storage
    assert '"aws:SecureTransport" = "false"' in storage
    assert 'status = "Enabled"' in storage


def test_artifact_bucket_has_no_browser_cors_surface() -> None:
    storage = read("storage.tf")
    assert "aws_s3_bucket_cors_configuration" not in storage
    assert "allowed_origins" not in storage


def test_public_dashboard_listener_requires_tls() -> None:
    variables = read("variables.tf")
    load_balancer = read("loadbalancer.tf")
    assert 'certificate_arn is required because browser sessions use Secure cookies.' in variables
    assert 'protocol          = "HTTPS"' in load_balancer
    assert 'ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"' in load_balancer
    assert 'protocol    = "HTTPS"' in load_balancer


def test_only_the_worker_security_group_reaches_the_worker_api() -> None:
    security = read("security.tf")
    internal = re.search(
        r'resource "aws_security_group_rule" "internal_lb_from_worker" \{(.*?)\n\}',
        security,
        re.DOTALL,
    )
    assert internal is not None
    assert "source_security_group_id = aws_security_group.worker.id" in internal.group(1)
    assert "cidr_blocks" not in internal.group(1)
    database = re.search(
        r'resource "aws_security_group_rule" "database_from_control_plane" \{(.*?)\n\}',
        security,
        re.DOTALL,
    )
    assert database is not None
    assert "source_security_group_id = aws_security_group.control_plane.id" in database.group(1)


def test_services_run_without_public_addresses() -> None:
    ecs = read("ecs.tf")
    assert ecs.count("assign_public_ip = false") == 2
    assert ecs.count("subnets          = aws_subnet.private[*].id") == 2


@pytest.mark.parametrize(
    ("action", "role"),
    [
        ("s3:PutObject", "control_plane"),
        ("sqs:SendMessage", "control_plane"),
    ],
)
def test_workers_hold_no_artifact_or_publication_permission(action: str, role: str) -> None:
    iam = read("iam.tf")
    worker = re.search(r'data "aws_iam_policy_document" "worker" \{(.*?)\n\}\n', iam, re.DOTALL)
    assert worker is not None
    assert action not in worker.group(1)
    owner = re.search(rf'data "aws_iam_policy_document" "{role}" \{{(.*?)\n\}}\n', iam, re.DOTALL)
    assert owner is not None
    assert action in owner.group(1)


def test_queue_visibility_matches_the_attempt_lease() -> None:
    from locus.worker import LEASE_SECONDS

    queue = read("queue.tf")
    assert f"visibility_timeout_seconds = {LEASE_SECONDS}" in queue
    assert "deadLetterTargetArn" in queue
