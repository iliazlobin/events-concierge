terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "7.40.0" }
  }
}

provider "google" {
  project                     = local.project
  region                      = var.platform_contract.region
  impersonate_service_account = var.impersonate_service_account
}

locals {
  project           = var.platform_contract.project_id
  namespace         = "events-concierge-dev"
  names             = toset(["api", "frontend", "temporal-transactional", "temporal-catalog", "request-starter", "notifier", "account-erasure", "change-delivery", "handoff-expiry", "lifecycle-invariants", "catalog-jobs", "migration", "stores", "development-admin", "ingestion-executor"])
  catalog_executors = toset(["ingestion-executor", "temporal-catalog"])
  secrets           = toset(["database-url", "migration-url", "app-role-password", "postgres-admin", "temporal-postgres-admin", "redis-password", "redis-url", "operator-database-url", "ingestion-executor-database-url", "operator-role-password", "ingestion-executor-role-password"])
  workload_emails   = { for name in local.names : name => "ec-dev-${name}@${local.project}.iam.gserviceaccount.com" }
}

# This state owns application resources only. The platform contract identifies external
# prerequisites; no shared resource, API enablement, project IAM, or node identity is adopted.
resource "google_artifact_registry_repository" "images" {
  project       = local.project
  location      = "us-west1"
  repository_id = "ec-dev"
  format        = "DOCKER"
  lifecycle { prevent_destroy = true }
}
resource "google_artifact_registry_repository_iam_member" "node" {
  project    = local.project
  location   = "us-west1"
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.platform_contract.cluster.node_service_account}"
}
resource "google_service_account" "workload" {
  for_each   = local.names
  project    = local.project
  account_id = "ec-dev-${each.key}"
}
resource "google_service_account_iam_member" "workload" {
  for_each           = local.names
  service_account_id = "projects/${local.project}/serviceAccounts/${local.workload_emails[each.key]}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.project}.svc.id.goog[${local.namespace}/events-concierge-${each.key}]"
  depends_on         = [google_service_account.workload]
}
resource "google_secret_manager_secret" "development" {
  for_each  = local.secrets
  project   = local.project
  secret_id = "ec-dev-${each.key}"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}
locals {
  secret_access = merge(
    { for s in ["postgres-admin", "temporal-postgres-admin", "redis-password"] : "stores/${s}" => { name = "stores", secret = s } },
    { for s in ["migration-url", "app-role-password", "operator-role-password", "ingestion-executor-role-password"] : "migration/${s}" => { name = "migration", secret = s } },
    { for s in ["operator-database-url", "database-url", "redis-url"] : "development-admin/${s}" => { name = "development-admin", secret = s } },
    { for pair in setproduct(local.catalog_executors, toset(["ingestion-executor-database-url", "redis-url"])) : "${pair[0]}/${pair[1]}" => { name = pair[0], secret = pair[1] } },
    { for pair in setproduct(setsubtract(local.names, setunion(local.catalog_executors, toset(["frontend", "stores", "migration", "development-admin"]))), toset(["database-url", "redis-url"])) : "${pair[0]}/${pair[1]}" => { name = pair[0], secret = pair[1] } }
  )
}
resource "google_secret_manager_secret_iam_member" "reader" {
  for_each   = local.secret_access
  project    = local.project
  secret_id  = google_secret_manager_secret.development[each.value.secret].secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:${local.workload_emails[each.value.name]}"
  depends_on = [google_service_account.workload]
}
resource "google_storage_bucket" "data" {
  for_each                    = toset(["payloads", "backups"])
  project                     = local.project
  name                        = "iz27-platform-dev-ec-${each.key}"
  location                    = "US-WEST1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = 604800 }
  lifecycle { prevent_destroy = true }
}
resource "google_storage_bucket_iam_member" "payloads" {
  for_each   = setsubtract(local.names, setunion(local.catalog_executors, toset(["frontend", "stores", "migration", "development-admin"])))
  bucket     = google_storage_bucket.data["payloads"].name
  role       = "roles/storage.objectUser"
  member     = "serviceAccount:${local.workload_emails[each.key]}"
  depends_on = [google_service_account.workload]
}
# The development admin retains the consumer object's existing capability for its app graph.
resource "google_storage_bucket_iam_member" "development_admin_payloads" {
  bucket     = google_storage_bucket.data["payloads"].name
  role       = "roles/storage.objectUser"
  member     = "serviceAccount:${local.workload_emails["development-admin"]}"
  depends_on = [google_service_account.workload]
  condition {
    title      = "development_consumer_payloads"
    expression = "resource.name.startsWith('projects/_/buckets/iz27-platform-dev-ec-payloads/objects/events-concierge/claim-check/v1/')"
  }
}
resource "google_storage_bucket_iam_member" "catalog_payloads" {
  for_each   = local.catalog_executors
  bucket     = google_storage_bucket.data["payloads"].name
  role       = "roles/storage.objectUser"
  member     = "serviceAccount:${local.workload_emails[each.key]}"
  depends_on = [google_service_account.workload]
  condition {
    title      = "development_catalog_payloads"
    expression = "resource.name.startsWith('projects/_/buckets/iz27-platform-dev-ec-payloads/objects/events-concierge/catalog/v1/')"
  }
}

# Bootstrap this app-owned state bucket locally, then migrate only this new root's state.
resource "google_storage_bucket" "state" {
  project                     = local.project
  name                        = "iz27-platform-dev-ec-state"
  location                    = "US-WEST1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = 604800 }
  lifecycle { prevent_destroy = true }
}
