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
    condition = var.public_origin_profile == "private_loopback_https" ? (
      var.project_id == "iz27-platform-dev" && var.public_origin == "https://localhost:14443"
      ) : (
      can(regex("^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", var.public_origin)) &&
      var.public_origin != "https://localhost"
    )
    error_message = "Use an exact remote HTTPS origin, or https://localhost:14443 with the private_loopback_https profile in iz27-platform-dev."
  }
}
variable "public_origin_profile" {
  type    = string
  default = "remote_https"
  validation {
    condition     = contains(["remote_https", "private_loopback_https"], var.public_origin_profile)
    error_message = "Choose the application's remote_https or private_loopback_https origin profile."
  }
}
variable "api_service_account" { type = string }
variable "erasure_service_account" { type = string }
variable "signup_enabled" {
  type        = bool
  default     = false
  description = "Enable only after providers, legal pages and the deployed account flow are verified."
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
