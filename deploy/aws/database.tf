resource "aws_db_subnet_group" "main" {
  name       = local.prefix
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = local.prefix }
}

resource "random_password" "database" {
  length  = 40
  special = false
}

resource "aws_db_instance" "main" {
  identifier     = local.prefix
  engine         = "postgres"
  engine_version = "17"
  instance_class = var.database_instance_class

  allocated_storage     = var.database_storage_gb
  max_allocated_storage = var.database_storage_gb * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "tracewake"
  username = "tracewake"
  password = random_password.database.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  backup_retention_period    = var.database_backup_retention_days
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true
  apply_immediately          = true

  deletion_protection = var.database_deletion_protection
  skip_final_snapshot = var.database_skip_final_snapshot
  final_snapshot_identifier = (
    var.database_skip_final_snapshot ? null : "${local.prefix}-final"
  )

  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = { Name = local.prefix }
}
