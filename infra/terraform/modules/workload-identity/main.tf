locals {
  workload_role_bindings = {
    for binding in flatten([
      for account_key, account in var.service_accounts : [
        for role in account.project_roles : {
          key         = "${account_key}:${role}"
          account_key = account_key
          role        = role
        }
      ]
    ]) : binding.key => binding
  }

  node_roles = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])
}

resource "google_service_account" "node" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-gke-node"
  display_name = "${var.name_prefix} GKE node"
  description  = "Node-system identity; application Pods use Workload Identity instead."
}

resource "google_project_iam_member" "node" {
  for_each = local.node_roles

  project = var.project_id
  role    = each.value
  member  = google_service_account.node.member
}

resource "google_service_account" "workload" {
  for_each = var.service_accounts

  project      = var.project_id
  account_id   = each.value.gcp_account_id
  display_name = "${var.name_prefix} ${each.key}"
  description  = "Workload Identity principal for ${each.value.kubernetes_service_account}."
}

resource "google_service_account_iam_member" "workload_identity" {
  for_each = var.service_accounts

  service_account_id = google_service_account.workload[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.kubernetes_namespace}/${each.value.kubernetes_service_account}]"
}

resource "google_project_iam_member" "workload" {
  for_each = local.workload_role_bindings

  project = var.project_id
  role    = each.value.role
  member  = google_service_account.workload[each.value.account_key].member
}
