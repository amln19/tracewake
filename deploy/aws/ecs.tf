resource "aws_ecr_repository" "control_plane" {
  name                 = "${local.prefix}/control-plane"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "worker" {
  name                 = "${local.prefix}/worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "control_plane" {
  name              = "/${local.prefix}/control-plane"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/${local.prefix}/worker"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name = local.prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  public_scheme   = var.certificate_arn == "" ? "http" : "https"
  public_base_url = var.public_base_url != "" ? var.public_base_url : "${local.public_scheme}://${aws_lb.public.dns_name}"
  worker_base_url = "http://${aws_lb.internal.dns_name}:8081"

  secret_arns = { for key, secret in aws_secretsmanager_secret.service : key => secret.arn }
}

resource "aws_ecs_task_definition" "control_plane" {
  family                   = "${local.prefix}-control-plane"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.control_plane_cpu
  memory                   = var.control_plane_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.control_plane.arn

  container_definitions = jsonencode([{
    name      = "control-plane"
    image     = "${aws_ecr_repository.control_plane.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [
      { containerPort = 8080, protocol = "tcp" },
      { containerPort = 8081, protocol = "tcp" },
    ]

    environment = [
      { name = "LOCUS_LISTEN_ADDR", value = "0.0.0.0:8080" },
      { name = "LOCUS_WORKER_LISTEN_ADDR", value = "0.0.0.0:8081" },
      { name = "LOCUS_ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "LOCUS_JOB_QUEUE_URL", value = aws_sqs_queue.jobs.url },
      { name = "LOCUS_PUBLIC_BASE_URL", value = local.public_base_url },
      { name = "LOCUS_WORKER_BASE_URL", value = local.worker_base_url },
      { name = "LOCUS_BOOTSTRAP_WORKSPACE", value = var.environment },
      { name = "AWS_REGION", value = var.region },
    ]

    secrets = [
      { name = "LOCUS_DATABASE_URL", valueFrom = local.secret_arns["database_url"] },
      { name = "LOCUS_TOKEN_PEPPER", valueFrom = local.secret_arns["token_pepper"] },
      { name = "LOCUS_WORKER_PEPPER", valueFrom = local.secret_arns["worker_pepper"] },
      { name = "LOCUS_BOOTSTRAP_TOKEN", valueFrom = local.secret_arns["tenant_token"] },
      { name = "LOCUS_WORKER_BOOTSTRAP_TOKEN", valueFrom = local.secret_arns["worker_token"] },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.control_plane.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "control-plane"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
    essential = true

    environment = [
      { name = "LOCUS_WORKER_URL", value = local.worker_base_url },
      { name = "LOCUS_JOB_QUEUE_URL", value = aws_sqs_queue.jobs.url },
      { name = "LOCUS_WORKER_BUILD", value = "${var.name}-${var.image_tag}" },
      { name = "AWS_REGION", value = var.region },
      { name = "AWS_DEFAULT_REGION", value = var.region },
    ]

    secrets = [
      { name = "LOCUS_WORKER_TOKEN", valueFrom = local.secret_arns["worker_token"] },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "control_plane" {
  name            = "${local.prefix}-control-plane"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.control_plane.arn
  desired_count   = var.control_plane_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.control_plane.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.public.arn
    container_name   = "control-plane"
    container_port   = 8080
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.internal.arn
    container_name   = "control-plane"
    container_port   = 8081
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60

  depends_on = [aws_lb_listener.public, aws_lb_listener.internal]
}

resource "aws_ecs_service" "worker" {
  name            = "${local.prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200
}
