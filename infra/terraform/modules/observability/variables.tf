variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "name_prefix" {
  type        = string
  description = "Environment-specific resource prefix."
}

variable "notification_email" {
  type        = string
  description = "Optional non-secret email notification channel."
  default     = ""
}

variable "cloud_sql_instance_name" {
  type        = string
  description = "Cloud SQL instance shown in the operations dashboard."
}

variable "redis_instance_name" {
  type        = string
  description = "Redis instance shown in the operations dashboard."
}

variable "gke_cluster_name" {
  type        = string
  description = "GKE cluster shown in the operations dashboard."
}
