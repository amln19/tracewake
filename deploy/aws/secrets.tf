resource "random_password" "token_pepper" {
  length  = 48
  special = false
}

resource "random_password" "worker_pepper" {
  length  = 48
  special = false
}

resource "random_id" "tenant_token_prefix" {
  byte_length = 8
}

resource "random_password" "tenant_token_secret" {
  length  = 48
  special = false
}

resource "random_id" "worker_token_prefix" {
  byte_length = 8
}

resource "random_password" "worker_token_secret" {
  length  = 48
  special = false
}

locals {
  database_url = format(
    "postgres://%s:%s@%s/%s?sslmode=require",
    aws_db_instance.main.username,
    urlencode(random_password.database.result),
    aws_db_instance.main.endpoint,
    aws_db_instance.main.db_name,
  )

  # Tokens carry a non-secret prefix so the control plane can look up one
  # verifier without scanning, and the secret half never leaves this state or
  # Secrets Manager.
  tenant_token = "tracewake_${random_id.tenant_token_prefix.hex}.${random_password.tenant_token_secret.result}"
  worker_token = "worker_${random_id.worker_token_prefix.hex}.${random_password.worker_token_secret.result}"

  secret_values = {
    database_url  = local.database_url
    token_pepper  = random_password.token_pepper.result
    worker_pepper = random_password.worker_pepper.result
    tenant_token  = local.tenant_token
    worker_token  = local.worker_token
  }
}

resource "aws_secretsmanager_secret" "service" {
  for_each = local.secret_values

  name                    = "${local.prefix}/${replace(each.key, "_", "-")}"
  recovery_window_in_days = 0

  tags = { Name = "${local.prefix}-${each.key}" }
}

resource "aws_secretsmanager_secret_version" "service" {
  for_each = local.secret_values

  secret_id     = aws_secretsmanager_secret.service[each.key].id
  secret_string = each.value
}
