variable "region" {
  description = "AWS region for the environment."
  type        = string
}

variable "name" {
  description = "Name prefix for every resource in the environment."
  type        = string
  default     = "locus"
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
  description = "ACM certificate for the public listener. Without it the load balancer serves plain HTTP, which is only acceptable for a private evaluation environment."
  type        = string
  default     = ""
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

variable "log_retention_days" {
  description = "CloudWatch log retention for both services."
  type        = number
  default     = 30
}
