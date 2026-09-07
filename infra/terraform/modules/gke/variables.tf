variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "name_prefix" {
  type        = string
  description = "Environment-specific resource prefix."
}

variable "location" {
  type        = string
  description = "Zonal control-plane location."
}

variable "node_locations" {
  type        = list(string)
  description = "Zones in which the node pool is spread."
}

variable "network_id" {
  type        = string
  description = "VPC resource ID."
}

variable "subnetwork_id" {
  type        = string
  description = "Subnetwork resource ID."
}

variable "pods_range_name" {
  type        = string
  description = "Named Pod secondary range."
}

variable "services_range_name" {
  type        = string
  description = "Named Service secondary range."
}

variable "node_service_account_email" {
  type        = string
  description = "Least-privilege GKE node identity."
}

variable "machine_type" {
  type        = string
  description = "Initial application node machine type."
  default     = "e2-standard-2"
}

variable "min_nodes_per_zone" {
  type        = number
  description = "Minimum nodes in each configured zone."
  default     = 1
}

variable "max_nodes_per_zone" {
  type        = number
  description = "Maximum nodes in each configured zone."
  default     = 2

  validation {
    condition     = var.max_nodes_per_zone >= var.min_nodes_per_zone
    error_message = "max_nodes_per_zone must be at least min_nodes_per_zone."
  }
}

variable "master_ipv4_cidr" {
  type        = string
  description = "Private control-plane CIDR."
  default     = "172.16.0.0/28"
}

variable "enable_private_endpoint" {
  type        = bool
  description = "Keep the Kubernetes API private; use a connected deploy runner."
  default     = true
}

variable "master_authorized_networks" {
  description = "Explicit API source ranges when a public endpoint is deliberately enabled."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "deletion_protection" {
  type        = bool
  description = "Protect the cluster from an accidental Terraform destroy."
  default     = true
}
