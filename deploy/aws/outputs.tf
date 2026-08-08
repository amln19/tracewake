output "public_base_url" {
  description = "Base URL for TRACEWAKE_REMOTE_URL."
  value       = local.public_base_url
}

output "control_plane_repository" {
  description = "ECR repository for the Go control-plane image."
  value       = aws_ecr_repository.control_plane.repository_url
}

output "worker_repository" {
  description = "ECR repository for the Python worker image."
  value       = aws_ecr_repository.worker.repository_url
}

output "artifact_bucket" {
  description = "Private versioned bucket holding bundles and results."
  value       = aws_s3_bucket.artifacts.bucket
}

output "job_queue_url" {
  description = "Job notification queue."
  value       = aws_sqs_queue.jobs.url
}

output "job_dead_letter_queue_url" {
  description = "Dead-letter queue for notifications that never succeeded."
  value       = aws_sqs_queue.jobs_dead_letter.url
}

output "tenant_token_secret" {
  description = "Secrets Manager name holding the bootstrap tenant token. Read it with the AWS CLI; it is never logged."
  value       = aws_secretsmanager_secret.service["tenant_token"].name
}

output "database_endpoint" {
  description = "Private RDS endpoint. It is not reachable from outside the VPC."
  value       = aws_db_instance.main.endpoint
}

output "cluster_name" {
  description = "ECS cluster running both services."
  value       = aws_ecs_cluster.main.name
}
