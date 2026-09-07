variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "name_prefix" {
  type        = string
  description = "Short, environment-specific resource prefix."
}

variable "region" {
  type        = string
  description = "Primary application region."
}

variable "subnet_cidr" {
  type        = string
  description = "Primary node subnet range."
}

variable "pods_cidr" {
  type        = string
  description = "GKE Pod secondary range."
}

variable "services_cidr" {
  type        = string
  description = "GKE Service secondary range."
}

variable "private_service_prefix_length" {
  type        = number
  description = "Prefix length reserved for Private Service Access."
  default     = 20

  validation {
    condition     = var.private_service_prefix_length >= 16 && var.private_service_prefix_length <= 24
    error_message = "Private Service Access must reserve a /16 through /24 range."
  }
}
