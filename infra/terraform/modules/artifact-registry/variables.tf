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
  description = "Artifact Registry region."
}

variable "reader_members" {
  type        = set(string)
  description = "IAM members allowed to pull deployment images."
  default     = []
}
