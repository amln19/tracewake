resource "aws_wafv2_web_acl" "public" {
  name  = "${local.prefix}-public"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  custom_response_body {
    key          = "rate-limited"
    content_type = "APPLICATION_JSON"
    content = jsonencode({
      error = {
        code    = "rate_limited"
        message = "request rate exceeded the configured deployment limit"
      }
    })
  }

  rule {
    name     = "per-ip-rate-limit"
    priority = 1

    action {
      block {
        custom_response {
          custom_response_body_key = "rate-limited"
          response_code            = 429
        }
      }
    }

    statement {
      rate_based_statement {
        aggregate_key_type    = "IP"
        evaluation_window_sec = var.public_rate_limit_window_seconds
        limit                 = var.public_rate_limit
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-public-rate-limit"
      sampled_requests_enabled   = false
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.prefix}-public-waf"
    sampled_requests_enabled   = false
  }
}

resource "aws_wafv2_web_acl_association" "public" {
  resource_arn = aws_lb.public.arn
  web_acl_arn  = aws_wafv2_web_acl.public.arn
}
