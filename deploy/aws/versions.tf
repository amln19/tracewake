terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.100.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
    }
  }

  # State holds database and token material, so it must live in a private
  # remote bucket chosen by the operator: supply bucket, key, and region with
  # `terraform init -backend-config=...`.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = var.name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
