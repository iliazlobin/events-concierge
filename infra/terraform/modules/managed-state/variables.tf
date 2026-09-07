variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "name_prefix" {
  type        = string
  description = "Environment-specific resource prefix."
}

variable "region" {
  type        = string
  description = "Region for managed state."
}

variable "network_id" {
  type        = string
  description = "VPC resource ID used for private service networking."
}

variable "database_name" {
  type        = string
  description = "Application database name."
  default     = "events"
}

variable "database_tier" {
  type        = string
  description = "Cloud SQL machine tier."
  default     = "db-custom-2-7680"
}

variable "database_disk_size_gb" {
  type        = number
  description = "Initial Cloud SQL SSD size."
  default     = 50
}

variable "redis_memory_size_gb" {
  type        = number
  description = "Memorystore capacity."
  default     = 5
}

variable "deletion_protection" {
  type        = bool
  description = "Protect durable services from accidental destroy."
  default     = true
}
