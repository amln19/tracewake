# Alarms are declared as data rather than HCL so the same file that provisions
# them can be checked against the metrics the services actually emit.
locals {
  alarm_specification = jsondecode(file("${path.module}/alarms.json"))

  control_plane_metric_namespace = "Locus/${var.environment}/ControlPlane"
  worker_metric_namespace        = "Locus/${var.environment}/Worker"

  alarm_namespaces = {
    "@control_plane" = local.control_plane_metric_namespace
    "@worker"        = local.worker_metric_namespace
  }

  alarm_targets = {
    "@jobs"                  = aws_sqs_queue.jobs.name
    "@jobs_dead_letter"      = aws_sqs_queue.jobs_dead_letter.name
    "@public_load_balancer"  = aws_lb.public.arn_suffix
    "@public_target_group"   = aws_lb_target_group.public.arn_suffix
    "@cluster"               = aws_ecs_cluster.main.name
    "@control_plane_service" = aws_ecs_service.control_plane.name
    "@worker_service"        = aws_ecs_service.worker.name
    "@database"              = aws_db_instance.main.identifier
  }

  alarms = { for alarm in local.alarm_specification.alarms : alarm.name => alarm }
}

resource "aws_cloudwatch_metric_alarm" "operational" {
  for_each = local.alarms

  alarm_name          = "${local.prefix}-${each.key}"
  alarm_description   = each.value.description
  namespace           = lookup(local.alarm_namespaces, each.value.namespace, each.value.namespace)
  metric_name         = each.value.metric_name
  dimensions          = { for key, value in each.value.dimensions : key => lookup(local.alarm_targets, value, value) }
  statistic           = each.value.statistic
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold
  period              = each.value.period_seconds
  evaluation_periods  = each.value.evaluation_periods
  treat_missing_data  = each.value.treat_missing_data

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions

  tags = { Name = "${local.prefix}-${each.key}" }
}
