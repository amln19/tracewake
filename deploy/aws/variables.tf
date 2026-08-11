variable "region" {
  description = "AWS region for the environment."
  type        = string
}

variable "name" {
  description = "Name prefix for every resource in the environment."
  type        = string
  default     = "tracewake"
}

variable "environment" {
  description = "Environment name used in tags and resource names."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "Address range for the environment VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "image_tag" {
  description = "Container image tag deployed for both services."
  type        = string
  default     = "latest"
}

variable "control_plane_count" {
  description = "Number of control-plane tasks."
  type        = number
  default     = 1
}

variable "worker_count" {
  description = "Number of Python worker tasks."
  type        = number
  default     = 1
}

variable "control_plane_cpu" {
  description = "Fargate CPU units for the control plane."
  type        = number
  default     = 512
}

variable "control_plane_memory" {
  description = "Fargate memory (MiB) for the control plane."
  type        = number
  default     = 1024
}

variable "worker_cpu" {
  description = "Fargate CPU units for a worker."
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for a worker. Bundle limits are 256 MiB."
  type        = number
  default     = 2048
}

variable "task_architecture" {
  description = "Fargate CPU architecture. Images must be built for it; ARM64 costs less per task."
  type        = string
  default     = "ARM64"
}

variable "database_instance_class" {
  description = "RDS instance class for authoritative hosted state."
  type        = string
  default     = "db.t4g.micro"
}

variable "database_storage_gb" {
  description = "Allocated RDS storage in gibibytes."
  type        = number
  default     = 20
}

variable "database_backup_retention_days" {
  description = "Automated backup retention for point-in-time recovery."
  type        = number
  default     = 7
}

variable "database_deletion_protection" {
  description = "Refuse to destroy the database. Enable once real tenant data exists."
  type        = bool
  default     = false
}

variable "database_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Keeps deploy and destroy repeatable in a disposable environment."
  type        = bool
  default     = true
}

variable "certificate_arn" {
  description = "ACM certificate for the HTTPS public listener. Browser sessions require TLS."
  type        = string

  validation {
    condition     = length(trimspace(var.certificate_arn)) > 0
    error_message = "certificate_arn is required because browser sessions use Secure cookies."
  }
}

variable "worker_certificate_arn" {
  description = "ACM certificate for the private worker HTTPS listener. Its names must cover worker_base_url or the internal load balancer DNS name."
  type        = string

  validation {
    condition     = length(trimspace(var.worker_certificate_arn)) > 0
    error_message = "worker_certificate_arn is required because worker credentials may only be sent over private HTTPS."
  }
}

variable "worker_base_url" {
  description = "Optional private HTTPS alias for the worker API. When empty, workers use the internal load balancer DNS name."
  type        = string
  default     = ""

  validation {
    condition     = var.worker_base_url == "" || startswith(var.worker_base_url, "https://")
    error_message = "worker_base_url must be an HTTPS URL."
  }
}

variable "worker_ca_pem" {
  description = "PEM trust anchor for a private or self-signed worker listener certificate. Leave empty for a publicly trusted certificate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "public_base_url" {
  description = "External base URL tenants reach. Defaults to the load balancer name over the listener protocol."
  type        = string
  default     = ""
}

variable "allowed_tenant_cidrs" {
  description = "Address ranges allowed to reach the public API."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "public_rate_limit" {
  description = "Maximum public requests allowed from one source IP during public_rate_limit_window_seconds. Set from the environment's measured legitimate traffic."
  type        = number

  validation {
    condition     = var.public_rate_limit >= 10 && floor(var.public_rate_limit) == var.public_rate_limit
    error_message = "public_rate_limit must be an integer of at least 10, the AWS WAF minimum."
  }
}

variable "public_rate_limit_window_seconds" {
  description = "AWS WAF rate-counting window in seconds. Choose a window that matches the environment's burst policy."
  type        = number

  validation {
    condition     = contains([60, 120, 300, 600], var.public_rate_limit_window_seconds)
    error_message = "public_rate_limit_window_seconds must be one of AWS WAF's supported windows: 60, 120, 300, or 600."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for both services."
  type        = number
  default     = 30
}

variable "alarm_actions" {
  description = "Targets notified when an operational alarm changes state. Alarm state is visible without one."
  type        = list(string)
  default     = []
}
