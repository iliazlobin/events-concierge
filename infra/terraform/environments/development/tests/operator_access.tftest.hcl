# Provider calls are mocked. Legacy retirement retains recovery data and access.
mock_provider "google" {}

run "private_operator_and_catalog_access" {
  command = plan

  assert {
    condition = toset([
      for access in values(local.secret_access) : access.secret
      if access.name == "development-admin"
    ]) == toset(["operator-database-url", "database-url", "redis-url"])
    error_message = "The private combined admin needs its own controller credential plus consumer reads, never executor or migration credentials."
  }
  assert {
    condition = alltrue([
      for name in ["ingestion-executor", "temporal-catalog"] :
      toset([for access in values(local.secret_access) : access.secret if access.name == name]) == toset(["ingestion-executor-database-url", "redis-url"])
    ])
    error_message = "Each catalog process may access only the executor database and Redis credentials."
  }
  assert {
    condition = alltrue([
      for access in values(local.secret_access) :
      !contains(["operator-role-password", "ingestion-executor-role-password"], access.secret) || access.name == "migration"
      ]) && alltrue([
      for name in ["operator-role-password", "ingestion-executor-role-password"] :
      contains(keys(google_secret_manager_secret_iam_member.reader), "migration/${name}")
    ])
    error_message = "Only the migration identity may read role bootstrap passwords."
  }
  assert {
    condition = alltrue([
      for name in ["development-admin", "ingestion-executor", "temporal-catalog"] :
      google_service_account.workload[name].account_id == "ec-dev-${name}" &&
      google_service_account_iam_member.workload[name].member == "serviceAccount:project-9c8cce04-f94d-40fc-aa6.svc.id.goog[events-concierge-dev/events-concierge-${name}]"
    ])
    error_message = "Admin and each catalog process must retain distinct Workload Identities bound to their own Kubernetes service accounts."
  }
  assert {
    condition = alltrue([
      for name in ["ingestion-executor", "temporal-catalog"] :
      !contains(keys(google_storage_bucket_iam_member.payloads), name) &&
      google_storage_bucket_iam_member.catalog_payloads[name].role == "roles/storage.objectUser" &&
      google_storage_bucket_iam_member.catalog_payloads[name].condition[0].expression == "resource.name.startsWith('projects/_/buckets/iz27-ec-dev-payloads/objects/events-concierge/catalog/v1/')"
    ])
    error_message = "Catalog object access must be prefix-limited and cannot retain a broad bucket grant."
  }
  assert {
    condition     = !contains(keys(google_storage_bucket_iam_member.payloads), "development-admin") && google_storage_bucket_iam_member.development_admin_payloads.condition[0].expression == "resource.name.startsWith('projects/_/buckets/iz27-ec-dev-payloads/objects/events-concierge/claim-check/v1/')"
    error_message = "The development admin's object capability must remain in the consumer prefix."
  }
  assert {
    condition = output.cluster == null && length(google_service_account.workload) == 15 && alltrue([
      for bucket in values(google_storage_bucket.data) :
      !bucket.force_destroy && bucket.public_access_prevention == "enforced" && bucket.uniform_bucket_level_access &&
      bucket.versioning[0].enabled && bucket.soft_delete_policy[0].retention_duration_seconds == 604800
    ])
    error_message = "Retirement must stop advertising a cluster while preserving private, recoverable application buckets and workload identities."
  }
  assert {
    condition     = length(google_storage_bucket.data) == 2 && length(google_secret_manager_secret.development) == 11 && google_artifact_registry_repository.images.repository_id == "ec-dev" && google_service_account.node.account_id == "ec-dev-gke-node"
    error_message = "Retirement must preserve both buckets, all secret containers, image history and the legacy node identity."
  }
}
