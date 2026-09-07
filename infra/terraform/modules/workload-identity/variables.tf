variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "name_prefix" {
  type        = string
  description = "Short environment prefix used by the node identity."
}

variable "kubernetes_namespace" {
  type        = string
  description = "Namespace containing the bound Kubernetes service accounts."
}

variable "service_accounts" {
  description = "Workload identities and their deliberately small project-level role sets."
  type = map(object({
    gcp_account_id             = string
    kubernetes_service_account = string
    project_roles              = set(string)
  }))
}
