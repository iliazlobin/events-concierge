variable "project_id" {
  description = "Project in which the application APIs are enabled."
  type        = string
}

variable "additional_services" {
  description = "Optional APIs required by explicitly enabled deployment profiles."
  type        = set(string)
  default     = []
}

variable "services" {
  description = "Google APIs required by the first production slice."
  type        = set(string)
  default = [
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "redis.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]
}
