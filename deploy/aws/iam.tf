data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for secret in aws_secretsmanager_secret.service : secret.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${local.prefix}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "control_plane" {
  name               = "${local.prefix}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# The control plane is the only identity that may write artifacts or publish
# notifications.
data "aws_iam_policy_document" "control_plane" {
  statement {
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  statement {
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [aws_s3_bucket.artifacts.arn]
  }

  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "${local.prefix}-control-plane"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Workers consume notifications and nothing else: object access arrives as
# short-lived URLs issued by the control plane.
data "aws_iam_policy_document" "worker" {
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "${local.prefix}-worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}
