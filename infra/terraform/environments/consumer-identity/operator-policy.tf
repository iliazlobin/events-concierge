# Non-secret policy configuration has an independent version history. Payloads are
# populated by an authorized operator outside Terraform and never enter its state.
data "google_project" "application" {
  project_id = var.project_id
}

resource "google_parameter_manager_parameter" "operator_rbac" {
  project         = var.project_id
  parameter_id    = "ec-operator-rbac"
  format          = "JSON"
  deletion_policy = "PREVENT"
  labels          = { purpose = "operator-rbac" }
  depends_on      = [google_project_service.identity]
  lifecycle { prevent_destroy = true }
}

locals {
  operator_policy_parameter = "projects/${data.google_project.application.number}/locations/global/parameters/${google_parameter_manager_parameter.operator_rbac.parameter_id}"
}

resource "google_project_iam_custom_role" "operator_policy_reader" {
  count       = var.operator_api_service_account == null ? 0 : 1
  project     = var.project_id
  role_id     = "ecOperatorPolicyReader"
  title       = "Events Concierge operator policy reader"
  permissions = ["parametermanager.parameterVersions.get"]
  depends_on  = [google_project_service.identity]
}

# Parameter Manager uses project-level IAM. Its resource attributes let this
# get-only grant restrict the workload to versions of this single parameter.
resource "google_project_iam_member" "operator_policy_reader" {
  count   = var.operator_api_service_account == null ? 0 : 1
  project = var.project_id
  role    = google_project_iam_custom_role.operator_policy_reader[0].name
  member  = "serviceAccount:${var.operator_api_service_account}"
  condition {
    title       = "operator_rbac_versions_only"
    description = "Read only immutable versions of the operator RBAC parameter."
    expression  = "resource.service == 'parametermanager.googleapis.com' && resource.type == 'parametermanager.googleapis.com/ParameterVersion' && resource.name.startsWith('${local.operator_policy_parameter}/versions/')"
  }
}
