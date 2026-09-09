terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "7.40.0" }
  }
}
provider "google" {
  project                     = local.project
  region                      = "us-west1"
  impersonate_service_account = var.impersonate_service_account
}
variable "impersonate_service_account" {
  type    = string
  default = null
}
variable "machine_type" {
  type    = string
  default = "e2-standard-2"
  validation {
    condition     = contains(["e2-standard-2", "e2-standard-4"], var.machine_type)
    error_message = "Only the reviewed 8 or 16 GiB development node is allowed."
  }
}
locals {
  project   = "project-9c8cce04-f94d-40fc-aa6"
  namespace = "events-concierge-dev"
  names     = toset(["api", "frontend", "temporal-transactional", "temporal-catalog", "request-starter", "notifier", "account-erasure", "change-delivery", "handoff-expiry", "lifecycle-invariants", "catalog-jobs", "migration", "stores"])
  secrets   = toset(["database-url", "migration-url", "app-role-password", "postgres-admin", "temporal-postgres-admin", "redis-password", "redis-url"])
}
resource "google_project_service" "api" {
  for_each           = toset(["container.googleapis.com", "artifactregistry.googleapis.com", "secretmanager.googleapis.com", "iamcredentials.googleapis.com", "storage.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com"])
  project            = local.project
  service            = each.value
  disable_on_destroy = false
}
data "google_compute_network" "existing" {
  name    = "iz27-dev"
  project = local.project
}
data "google_compute_subnetwork" "existing" {
  name    = "iz27-dev-usw1"
  project = local.project
  region  = "us-west1"
}
resource "google_service_account" "node" {
  project    = local.project
  account_id = "ec-dev-gke-node"
}
resource "google_project_iam_member" "node" {
  for_each = toset(["roles/container.defaultNodeServiceAccount"])
  project  = local.project
  role     = each.value
  member   = google_service_account.node.member
}
resource "google_container_cluster" "development" {
  project                  = local.project
  name                     = "ec-dev"
  location                 = "us-west1-a"
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = true
  network                  = data.google_compute_network.existing.id
  subnetwork               = data.google_compute_subnetwork.existing.id
  networking_mode          = "VPC_NATIVE"
  datapath_provider        = "ADVANCED_DATAPATH"
  release_channel { channel = "STABLE" }
  workload_identity_config { workload_pool = "${local.project}.svc.id.goog" }
  ip_allocation_policy {
    cluster_secondary_range_name  = "gke-pods"
    services_secondary_range_name = "gke-services"
  }
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true
  }
  control_plane_endpoints_config {
    dns_endpoint_config { allow_external_traffic = true }
    ip_endpoints_config { enabled = false }
  }
  secret_manager_config { enabled = true }
  logging_config { enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"] }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus { enabled = false }
  }
  addons_config {
    http_load_balancing { disabled = true }
  }
  resource_labels = { environment = "development", service = "events-concierge" }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.google_compute_subnetwork.existing.ip_cidr_range == "10.20.0.0/20" && data.google_compute_subnetwork.existing.private_ip_google_access
      error_message = "Existing foundation subnet contract changed."
    }
  }
  depends_on = [google_project_service.api, google_project_iam_member.node]
}
resource "google_container_node_pool" "development" {
  project        = local.project
  name           = "development"
  cluster        = google_container_cluster.development.name
  location       = "us-west1-a"
  node_locations = ["us-west1-a"]
  node_count     = 1
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 30
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
    metadata = { disable-legacy-endpoints = "true" }
  }
}
resource "google_artifact_registry_repository" "images" {
  project       = local.project
  location      = "us-west1"
  repository_id = "ec-dev"
  format        = "DOCKER"
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}
resource "google_artifact_registry_repository_iam_member" "node" {
  project    = local.project
  location   = "us-west1"
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.node.member
}
resource "google_service_account" "workload" {
  for_each   = local.names
  project    = local.project
  account_id = "ec-dev-${each.key}"
}
resource "google_service_account_iam_member" "workload" {
  for_each           = local.names
  service_account_id = google_service_account.workload[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.project}.svc.id.goog[${local.namespace}/events-concierge-${each.key}]"
  depends_on         = [google_container_cluster.development]
}
resource "google_secret_manager_secret" "development" {
  for_each  = local.secrets
  project   = local.project
  secret_id = "ec-dev-${each.key}"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}
locals {
  secret_access = merge(
    { for s in ["postgres-admin", "temporal-postgres-admin", "redis-password"] : "stores/${s}" => { name = "stores", secret = s } },
    { for s in ["migration-url", "app-role-password"] : "migration/${s}" => { name = "migration", secret = s } },
    { for pair in setproduct(setsubtract(local.names, toset(["frontend", "stores", "migration"])), toset(["database-url", "redis-url"])) : "${pair[0]}/${pair[1]}" => { name = pair[0], secret = pair[1] } }
  )
}
resource "google_secret_manager_secret_iam_member" "reader" {
  for_each  = local.secret_access
  project   = local.project
  secret_id = google_secret_manager_secret.development[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.workload[each.value.name].member
}
resource "google_storage_bucket" "data" {
  for_each                    = toset(["payloads", "backups"])
  project                     = local.project
  name                        = "iz27-ec-dev-${each.key}"
  location                    = "US-WEST1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = 604800 }
  lifecycle { prevent_destroy = true }
}
resource "google_storage_bucket_iam_member" "payloads" {
  for_each = setsubtract(local.names, toset(["frontend", "stores", "migration"]))
  bucket   = google_storage_bucket.data["payloads"].name
  role     = "roles/storage.objectUser"
  member   = google_service_account.workload[each.key].member
}
output "cluster" { value = google_container_cluster.development.name }
output "namespace" { value = local.namespace }
output "service_accounts" { value = { for k, v in google_service_account.workload : k => v.email } }
output "buckets" { value = { for k, v in google_storage_bucket.data : k => v.name } }
