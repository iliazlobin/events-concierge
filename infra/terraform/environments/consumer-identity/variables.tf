variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use the reviewed application GCP project ID."
  }
}
variable "public_origin" {
  type = string
  validation {
    condition     = can(regex("^https://([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]([a-z0-9-]*[a-z0-9])?$", var.public_origin))
    error_message = "Use an exact HTTPS domain origin without a port, path, wildcard or IP address."
  }
}
variable "api_service_account" { type = string }
variable "erasure_service_account" { type = string }
variable "signup_enabled" {
  type        = bool
  default     = false
  description = "Enable only for an approved pilot or release; provider, legal-mode and deployed account acceptance remain required."
}
variable "foundation_owned_services" {
  type        = set(string)
  default     = []
  description = "Supporting APIs already enabled and owned by the foundation state; never adopt them here."
  validation {
    condition = alltrue([for service in var.foundation_owned_services :
    contains(["iam.googleapis.com", "secretmanager.googleapis.com"], service)])
    error_message = "Only IAM and Secret Manager supporting APIs can remain foundation-owned."
  }
}
variable "operator_backend_service" {
  type        = string
  default     = null
  description = "Existing IAP-protected operator backend name, supplied by the edge owner."
}
variable "operator_iap_member" {
  type        = string
  default     = null
  sensitive   = true
  description = "Approved operator user IAM member, supplied privately only with an existing IAP backend. This grant is separate from application RBAC."
  validation {
    condition = var.operator_backend_service == null ? var.operator_iap_member == null : can(regex(
      "^user:[^\\s@]+@[^\\s@]+\\.[^\\s@]+$", var.operator_iap_member
    ))
    error_message = "An operator backend requires one explicit user IAM member; omit the member when no backend is supplied."
  }
}
variable "operator_api_service_account" {
  type        = string
  default     = null
  description = "Dedicated operator API workload identity to grant read-only RBAC parameter access. Null leaves runtime access unconfigured."
  validation {
    condition = var.operator_api_service_account == null ? true : (
      var.operator_api_service_account != var.api_service_account &&
      var.operator_api_service_account != var.erasure_service_account &&
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@${var.project_id}\\.iam\\.gserviceaccount\\.com$", var.operator_api_service_account))
    )
    error_message = "Use the dedicated operator API service account in this application project."
  }
}
variable "impersonate_service_account" {
  type    = string
  default = null
}
