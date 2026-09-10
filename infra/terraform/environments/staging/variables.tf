variable "project_id" {
  type        = string
  description = "Existing GCP project dedicated to staging."
}

variable "operator_enabled" {
  type        = bool
  description = "Provision isolated hosted operator and ingestion executor identities and empty secret containers. Enable Helm separately after credentials and IAP are provisioned."
  default     = false
}

variable "catalog_claim_check_prefix" {
  type        = string
  description = "Dedicated object prefix granted to catalog executors; must match Helm operator.executorClaimCheckPrefix."
  default     = "events-concierge/catalog/claim-check/v1"

  validation {
    condition     = can(regex("^events-concierge/catalog/[a-zA-Z0-9/_-]+$", var.catalog_claim_check_prefix)) && !endswith(var.catalog_claim_check_prefix, "/")
    error_message = "Catalog claim checks must use a dedicated events-concierge/catalog/ prefix without a trailing slash."
  }
}

variable "name_prefix" {
  type        = string
  description = "Short lowercase prefix used for every staging resource."
  default     = "ec-stg"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,12}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-14 lowercase letters, digits, or hyphens."
  }
}

variable "region" {
  type        = string
  description = "Primary staging region."
  default     = "us-west1"
}

variable "control_plane_zone" {
  type        = string
  description = "Initial zonal GKE control plane."
  default     = "us-west1-a"
}

variable "node_locations" {
  type        = list(string)
  description = "Explicit zones for application nodes."
  default     = ["us-west1-a", "us-west1-b", "us-west1-c"]
}

variable "kubernetes_namespace" {
  type        = string
  description = "Application namespace bound through Workload Identity."
  default     = "events-concierge-staging"
}

variable "claim_check_bucket_name" {
  type        = string
  description = "Globally unique GCS bucket name. Do not put credentials in this value."
}

variable "subnet_cidr" {
  type        = string
  description = "GKE node subnet."
  default     = "10.40.0.0/20"
}

variable "pods_cidr" {
  type        = string
  description = "GKE Pod secondary range."
  default     = "10.44.0.0/14"
}

variable "services_cidr" {
  type        = string
  description = "GKE Service secondary range."
  default     = "10.48.0.0/20"
}

variable "master_ipv4_cidr" {
  type        = string
  description = "Private GKE control-plane range."
  default     = "172.16.0.0/28"
}

variable "enable_private_endpoint" {
  type        = bool
  description = "Require a network-connected deploy runner to reach the Kubernetes API."
  default     = true
}

variable "master_authorized_networks" {
  description = "Explicit sources if the private endpoint is disabled. Never use 0.0.0.0/0."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []

  validation {
    condition = alltrue([
      for network in var.master_authorized_networks :
      can(cidrhost(network.cidr_block, 0)) && !contains(["0.0.0.0/0", "::/0"], network.cidr_block)
    ])
    error_message = "Each authorized network must be a valid narrow CIDR; world-open IPv4/IPv6 ranges are forbidden."
  }
}

variable "notification_email" {
  type        = string
  description = "Required staging operations notification email; not a credential."

  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.notification_email))
    error_message = "notification_email must be a non-empty email address so alert policies are not silent."
  }
}

variable "deletion_protection" {
  type        = bool
  description = "Protect GKE and managed state from accidental destruction."
  default     = true
}

variable "gke_machine_type" {
  type        = string
  description = "Bootstrap node shape; revise only after staging measurements."
  default     = "e2-standard-2"
}

variable "gke_max_nodes_per_zone" {
  type        = number
  description = "Bounded node-pool autoscaling ceiling per zone."
  default     = 2

  validation {
    condition     = var.gke_max_nodes_per_zone >= 1 && var.gke_max_nodes_per_zone <= 5
    error_message = "Staging allows between one and five nodes per zone."
  }
}

variable "database_tier" {
  type        = string
  description = "Cloud SQL staging tier."
  default     = "db-custom-2-7680"
}

variable "redis_memory_size_gb" {
  type        = number
  description = "Memorystore staging capacity."
  default     = 5
}
