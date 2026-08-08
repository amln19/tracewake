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
    from tracewake.worker import LEASE_SECONDS

    queue = read("queue.tf")
    assert f"visibility_timeout_seconds = {LEASE_SECONDS}" in queue
    assert "deadLetterTargetArn" in queue


def alarms() -> list[dict[str, object]]:
    import json

    specification = json.loads(read("alarms.json"))
    assert specification["specification_version"] == 1
    return specification["alarms"]


def test_alarm_definitions_are_well_formed() -> None:
    names = set()
    for alarm in alarms():
        name = alarm["name"]
        assert name not in names, name
        names.add(name)
        assert alarm["statistic"] in {"Sum", "Average", "Maximum", "Minimum", "SampleCount"}
        assert alarm["comparison_operator"] in {
            "GreaterThanThreshold",
            "GreaterThanOrEqualToThreshold",
            "LessThanThreshold",
            "LessThanOrEqualToThreshold",
        }
        assert alarm["treat_missing_data"] in {"breaching", "notBreaching", "missing", "ignore"}
        assert alarm["period_seconds"] >= 60 and alarm["period_seconds"] % 60 == 0
        assert alarm["evaluation_periods"] >= 1
        assert len(alarm["description"]) > 40, name
    assert names


def test_every_alarm_is_provisioned_from_its_definition() -> None:
    observability = read("observability.tf")
    assert 'for_each = local.alarms' in observability
    assert 'jsondecode(file("${path.module}/alarms.json"))' in observability
    for symbol in {
        value
        for alarm in alarms()
        for value in alarm["dimensions"].values()  # type: ignore[union-attr]
        if str(value).startswith("@")
    }:
        assert f'"{symbol}"' in observability, symbol
    for namespace in {alarm["namespace"] for alarm in alarms() if str(alarm["namespace"]).startswith("@")}:
        assert f'"{namespace}"' in observability, namespace


def test_worker_alarms_watch_metrics_the_worker_emits() -> None:
    from tracewake.telemetry import OPERATIONS, OUTCOMES, STAGES

    allowed = {
        "WorkerJobs": {"Operation": OPERATIONS, "Outcome": OUTCOMES},
        "WorkerStageMillis": {"Operation": OPERATIONS, "Stage": STAGES},
    }
    checked = 0
    for alarm in alarms():
        if alarm["namespace"] != "@worker":
            continue
        checked += 1
        dimensions = allowed[str(alarm["metric_name"])]
        assert set(alarm["dimensions"]) == set(dimensions), alarm["name"]  # type: ignore[arg-type]
        for key, value in alarm["dimensions"].items():  # type: ignore[union-attr]
            assert value in dimensions[key], (alarm["name"], key, value)
    assert checked


def test_services_report_the_environment_they_run_in() -> None:
    ecs = read("ecs.tf")
    for setting in (
        'name = "TRACEWAKE_ENVIRONMENT", value = var.environment',
        'name = "TRACEWAKE_METRIC_NAMESPACE", value = local.control_plane_metric_namespace',
        'name = "TRACEWAKE_WORKER_METRIC_NAMESPACE", value = local.worker_metric_namespace',
    ):
        assert setting in ecs, setting


def test_platform_alarms_have_the_metric_source_they_need() -> None:
    """An alarm on a metric the deployment never publishes is worse than none.

    Custom-namespace alarms are checked against the emitted metric stream by the
    control-plane tests. Platform namespaces need their source switched on here.
    """
    namespaces = {str(alarm["namespace"]) for alarm in alarms()}
    if "ECS/ContainerInsights" in namespaces:
        assert 'value = "enabled"' in read("ecs.tf").split('"containerInsights"', 1)[1][:120], (
            "an alarm reads ECS/ContainerInsights but the cluster does not enable it"
        )
