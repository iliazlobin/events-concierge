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
  description = "Region for the bucket, key ring, and secret replicas."
}

variable "bucket_name" {
  type        = string
  description = "Globally unique claim-check bucket name."

  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63
    error_message = "bucket_name must be a valid 3-63 character GCS name."
  }
}

variable "runtime_members" {
  type        = set(string)
  description = "Workload IAM members allowed to use the claim-check bucket and envelope key."
  default     = []
}

variable "catalog_executor_members" {
  type        = set(string)
  description = "Catalog identities allowed to read and write only the catalog object prefix, without application envelope-key access."
  default     = []
}

variable "catalog_claim_check_prefix" {
  type        = string
  description = "Exact object prefix, excluding the final slash, used by catalog-only executors."
  default     = "events-concierge/catalog/claim-check/v1"

  validation {
    condition     = can(regex("^events-concierge/catalog/[a-zA-Z0-9/_-]+$", var.catalog_claim_check_prefix)) && !endswith(var.catalog_claim_check_prefix, "/")
    error_message = "Catalog claim checks require a dedicated events-concierge/catalog/ prefix without a trailing slash."
  }
}

variable "secret_names" {
  type        = set(string)
  description = "Secret Manager containers only; values are populated outside Terraform."
}

variable "secret_accessors" {
  type        = map(set(string))
  description = "Secret name to the exact IAM members allowed to read its versions."
  default     = {}
}

variable "claim_check_ttl_days" {
  type        = number
  description = "Automatic cleanup ceiling; tenant erasure may delete earlier."
  default     = 30
}

variable "deletion_protection" {
  type        = bool
  description = "Prevent accidental bucket destruction through force_destroy."
  default     = true
}
