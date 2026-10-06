mock_provider "google" {}

variables {
  platform_contract = {
    schema_version = 1
    project_id     = "iz27-platform-dev"
    region         = "us-west1"
    cluster = {
      name                   = "platform-dev"
      location               = "us-west1-a"
      workload_identity_pool = "iz27-platform-dev.svc.id.goog"
      node_service_account   = "platform-dev-node@iz27-platform-dev.iam.gserviceaccount.com"
    }
    storage = {
      class       = "shared-retain"
      provisioner = "pd.csi.storage.gke.io"
      zone        = "us-west1-a"
    }
  }
}

run "app_resources_and_external_platform_contract" {
  command = plan
  assert {
    condition = length(google_service_account.workload) == 15 && alltrue([
      for name, account in google_service_account.workload : account.account_id == "ec-dev-${name}" && account.project == "iz27-platform-dev"
    ])
    error_message = "Only the fifteen application identities belong in this root."
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.node.member == "serviceAccount:platform-dev-node@iz27-platform-dev.iam.gserviceaccount.com" && google_artifact_registry_repository_iam_member.node.role == "roles/artifactregistry.reader"
    error_message = "The external platform node identity may only read the application repository."
  }
  assert {
    condition = alltrue([
      for name in ["ingestion-executor", "temporal-catalog"] :
      toset([for access in values(local.secret_access) : access.secret if access.name == name]) == toset(["ingestion-executor-database-url", "redis-url"]) &&
      google_service_account_iam_member.workload[name].member == "serviceAccount:iz27-platform-dev.svc.id.goog[events-concierge-dev/events-concierge-${name}]" &&
      google_storage_bucket_iam_member.catalog_payloads[name].condition[0].expression == "resource.name.startsWith('projects/_/buckets/iz27-platform-dev-ec-payloads/objects/events-concierge/catalog/v1/')"
    ])
    error_message = "Catalog executor identity, credentials and object prefix must remain isolated."
  }
  assert {
    condition = alltrue([
      for access in values(local.secret_access) :
      !contains(["migration-url", "app-role-password", "operator-role-password", "ingestion-executor-role-password"], access.secret) || access.name == "migration"
    ])
    error_message = "Only the migration identity may access bootstrap credentials."
  }
  assert {
    condition = alltrue([
      for name, bucket in google_storage_bucket.data :
      bucket.name == "iz27-platform-dev-ec-${name}" && bucket.public_access_prevention == "enforced" && bucket.uniform_bucket_level_access && !bucket.force_destroy && bucket.versioning[0].enabled
    ])
    error_message = "Application data stays private and protected in new buckets."
  }
  assert {
    condition     = google_storage_bucket.media.name == "iz27-platform-dev-ec-media" && google_storage_bucket.media.public_access_prevention == "enforced" && google_storage_bucket.media.uniform_bucket_level_access && !google_storage_bucket.media.force_destroy && !google_storage_bucket.media.versioning[0].enabled && google_storage_bucket.media.soft_delete_policy[0].retention_duration_seconds == 0 && length(google_storage_bucket_iam_member.media_objects) == 3 && length(google_storage_bucket_iam_member.media_policy) == 3
    error_message = "Media must be durable across replicas, private, and erasable without hidden retention."
  }
  assert {
    condition     = output.cluster == "platform-dev" && output.namespace == "events-concierge-dev" && output.storage_class == "shared-retain"
    error_message = "Release tooling must consume the shared application landing target."
  }
  assert {
    condition     = google_storage_bucket.state.name == "iz27-platform-dev-ec-state" && google_storage_bucket.state.public_access_prevention == "enforced" && google_storage_bucket.state.versioning[0].enabled && !google_storage_bucket.state.force_destroy
    error_message = "Only this new application's state may move to its private protected state bucket."
  }
}

run "dedicated_private_operator" {
  command = plan
  assert {
    condition = (
      google_service_account.operator_api.account_id == "ec-dev-operator-api" &&
      google_service_account.operator_api.project == "iz27-platform-dev" &&
      google_service_account_iam_member.operator_api_workload.role == "roles/iam.workloadIdentityUser" &&
      google_service_account_iam_member.operator_api_workload.service_account_id == "projects/iz27-platform-dev/serviceAccounts/ec-dev-operator-api@iz27-platform-dev.iam.gserviceaccount.com" &&
      google_service_account_iam_member.operator_api_workload.member == "serviceAccount:iz27-platform-dev.svc.id.goog[events-concierge-dev/events-concierge-operator-api]" &&
      google_secret_manager_secret_iam_member.operator_api_database.secret_id == "ec-dev-operator-database-url" &&
      google_secret_manager_secret_iam_member.operator_api_database.role == "roles/secretmanager.secretAccessor" &&
      google_secret_manager_secret_iam_member.operator_api_database.member == "serviceAccount:ec-dev-operator-api@iz27-platform-dev.iam.gserviceaccount.com" &&
      !contains(local.names, "operator-api") &&
      !contains(local.media_workloads, "operator-api") &&
      length([for access in values(local.secret_access) : access if access.name == "operator-api"]) == 0 &&
      length(local.secret_access) == 32 &&
      length(google_storage_bucket_iam_member.payloads) == 9 &&
      output.service_accounts["operator-api"] == "ec-dev-operator-api@iz27-platform-dev.iam.gserviceaccount.com"
    )
    error_message = "The private operator receives only its controller DSN and exact workload binding."
  }
}

run "operator_only_tls_recovery_containers" {
  command = plan
  assert {
    condition = (
      toset(keys(google_secret_manager_secret.tls_recovery)) == toset(["postgres", "redis", "temporal"]) &&
      alltrue([
        for store, secret in google_secret_manager_secret.tls_recovery :
        secret.project == "iz27-platform-dev" &&
        secret.secret_id == "ec-dev-${store}-ca-recovery-g1" &&
        secret.labels == tomap({ purpose = "ca-recovery", generation = "1" }) &&
        secret.version_destroy_ttl == "2592000s" &&
        length(secret.replication[0].auto) == 1 &&
        length(secret.replication[0].user_managed) == 0 &&
        !contains(local.secrets, "${store}-ca-recovery-g1") &&
        alltrue([
          for reader in google_secret_manager_secret_iam_member.reader :
          reader.secret_id != secret.secret_id
        ]) &&
        google_secret_manager_secret_iam_member.operator_api_database.secret_id != secret.secret_id
      ])
    )
    error_message = "Three non-expiring recovery containers require a 30-day version destruction delay and no workload access."
  }
}

run "reject_legacy_platform_contract" {
  command = plan
  variables {
    platform_contract = {
      schema_version = 1
      project_id     = "project-9c8cce04-f94d-40fc-aa6"
      region         = "us-west1"
      cluster = {
        name                   = "ec-dev"
        location               = "us-west1-a"
        workload_identity_pool = "project-9c8cce04-f94d-40fc-aa6.svc.id.goog"
        node_service_account   = "ec-dev-gke-node@project-9c8cce04-f94d-40fc-aa6.iam.gserviceaccount.com"
      }
      storage = {
        class       = "ec-dev-retain"
        provisioner = "pd.csi.storage.gke.io"
        zone        = "us-west1-a"
      }
    }
  }
  expect_failures = [var.platform_contract]
}
