# All provider calls are mocked; both runs only plan and never create infrastructure.
mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/events-validation/serviceAccounts/mock-user@events-validation.iam.gserviceaccount.com"
      email  = "mock-user@events-validation.iam.gserviceaccount.com"
      member = "serviceAccount:mock-user@events-validation.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_compute_network" {
    defaults = {
      id = "projects/events-validation/global/networks/validation"
    }
  }
  mock_resource "google_kms_key_ring" {
    defaults = {
      id = "projects/events-validation/locations/us-west1/keyRings/validation"
    }
  }
  mock_resource "google_kms_crypto_key" {
    defaults = {
      id = "projects/events-validation/locations/us-west1/keyRings/validation/cryptoKeys/validation"
    }
  }
  mock_data "google_storage_project_service_account" {
    defaults = {
      member = "serviceAccount:service-123@gs-project-accounts.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}

variables {
  project_id              = "events-validation"
  claim_check_bucket_name = "events-validation-claim-check"
  notification_email      = "validation@example.com"
}

run "operator_disabled" {
  command = plan

  assert {
    condition     = !contains(keys(local.service_accounts), "operator_api") && !contains(keys(local.service_accounts), "ingestion_executor")
    error_message = "The default foundation must not create hosted operator identities."
  }
  assert {
    condition     = length(local.operator_secret_names) == 0 && length(local.operator_members) == 0 && length(local.executor_members) == 0
    error_message = "Operator secrets and authority must remain opt-in."
  }
  assert {
    condition     = output.operator_helm_values == null
    error_message = "Disabled operator infrastructure cannot advertise provisioned Helm identities."
  }
}

run "operator_enabled" {
  command = plan
  variables {
    operator_enabled = true
  }

  assert {
    condition     = local.service_accounts.operator_api.kubernetes_service_account == "events-concierge-operator-api" && local.service_accounts.ingestion_executor.kubernetes_service_account == "events-concierge-ingestion-executor" && local.service_accounts.operator_api.gcp_account_id != local.service_accounts.ingestion_executor.gcp_account_id
    error_message = "Controller and executor Workload Identity bindings must remain distinct and match Helm."
  }
  assert {
    condition = alltrue([
      for key in ["operator_api", "ingestion_executor"] :
      toset(local.service_accounts[key].project_roles) == toset(["roles/cloudsql.client"])
    ])
    error_message = "Operator identities must not receive broad project storage, secret, or KMS roles."
  }
  assert {
    condition     = local.secret_accessors["operator-database-url"] == local.operator_members && local.secret_accessors["ingestion-executor-database-url"] == local.executor_members
    error_message = "Each database secret must be readable only by its own profile."
  }
  assert {
    condition = alltrue([
      for name, members in local.secret_accessors :
      !contains(members, one(local.operator_members)) || name == "operator-database-url"
    ])
    error_message = "Controller identity must not read provider, consumer, or executor credentials."
  }
  assert {
    condition = toset([
      for name, members in local.secret_accessors : name
      if contains(members, one(local.executor_members))
    ]) == toset(["ingestion-executor-database-url", "redis-url", "redis-ca-certificate", "temporal-api-key"])
    error_message = "Executor identity may read only its own DB, Redis/CA, and Temporal credentials."
  }
  assert {
    condition     = length(setintersection(local.runtime_members, setunion(local.operator_members, local.executor_members))) == 0 && length(setintersection(local.migration_members, setunion(local.operator_members, local.executor_members))) == 0
    error_message = "Operator identities cannot inherit broad runtime bucket/KMS or migration access."
  }
}
