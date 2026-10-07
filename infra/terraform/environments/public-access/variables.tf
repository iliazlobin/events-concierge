variable "impersonate_service_account" {
  type        = string
  default     = null
  description = "Explicitly approved plan/apply identity; null uses the verified operator account."
}

variable "enable_cloud_dns" {
  type        = bool
  default     = false
  description = "Manage only the events.iliazlobin.com public child zone and its A/validation CNAME; parent delegation is separate."
}
