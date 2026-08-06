resource "aws_sqs_queue" "jobs_dead_letter" {
  name                      = "${local.prefix}-jobs-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = { Name = "${local.prefix}-jobs-dlq" }
}

# Visibility matches the database lease: a message stays hidden only as long as
# a worker could still hold the attempt it refers to.
resource "aws_sqs_queue" "jobs" {
  name                       = "${local.prefix}-jobs"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 10
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dead_letter.arn
    maxReceiveCount     = 5
  })

  tags = { Name = "${local.prefix}-jobs" }
}

resource "aws_sqs_queue_redrive_allow_policy" "jobs_dead_letter" {
  queue_url = aws_sqs_queue.jobs_dead_letter.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}
