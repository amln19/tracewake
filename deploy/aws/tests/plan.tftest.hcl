mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "313951300823"
    }
  }

  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-2a", "us-east-2b"]
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

run "complete_plan" {
  command = plan

  variables {
    region                           = "us-east-2"
    image_tag                        = "0000000000000000000000000000000000000001"
    certificate_arn                  = "arn:aws:acm:us-east-2:313951300823:certificate/00000000-0000-0000-0000-000000000000"
    worker_certificate_arn           = "arn:aws:acm:us-east-2:313951300823:certificate/00000000-0000-0000-0000-000000000001"
    public_rate_limit                = 100
    public_rate_limit_window_seconds = 60
  }
}
