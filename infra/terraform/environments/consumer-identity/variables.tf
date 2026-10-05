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
