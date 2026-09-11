variable "impersonate_service_account" {
  type    = string
  default = null
}

variable "platform_contract" {
  description = "Reviewed non-secret platform/gke platform_contract output. Installation/readiness must be verified separately before app deployment."
  type = object({
    schema_version = number
    project_id     = string
    region         = string
    cluster = object({
      name                   = string
      location               = string
      workload_identity_pool = string
      node_service_account   = string
    })
    storage = object({
      class       = string
      provisioner = string
      zone        = string
    })
  })
  validation {
    condition = (
      var.platform_contract.schema_version == 1 &&
      var.platform_contract.project_id == "iz27-platform-dev" &&
      var.platform_contract.region == "us-west1" &&
      var.platform_contract.cluster.name == "platform-dev" &&
      var.platform_contract.cluster.location == "us-west1-a" &&
      var.platform_contract.cluster.workload_identity_pool == "iz27-platform-dev.svc.id.goog" &&
      var.platform_contract.cluster.node_service_account == "platform-dev-node@iz27-platform-dev.iam.gserviceaccount.com" &&
      var.platform_contract.storage.class == "shared-retain" &&
      var.platform_contract.storage.provisioner == "pd.csi.storage.gke.io" &&
      var.platform_contract.storage.zone == "us-west1-a"
    )
    error_message = "The shared development platform contract changed; review the project, cluster, node identity and retained storage boundary before landing the app."
  }
}
