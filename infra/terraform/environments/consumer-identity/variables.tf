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
variable "impersonate_service_account" {
  type    = string
  default = null
}
