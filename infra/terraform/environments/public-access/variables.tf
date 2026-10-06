variable "impersonate_service_account" {
  type        = string
  default     = null
  description = "Explicitly approved plan/apply identity; null uses the verified operator account."
}
