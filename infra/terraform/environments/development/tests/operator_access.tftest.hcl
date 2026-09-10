# Provider calls are mocked. This plans the existing private development profile only.
mock_provider "google" {
  mock_data "google_compute_subnetwork" {
    defaults = {
      ip_cidr_range            = "10.20.0.0/20"
      private_ip_google_access = true
    }
  }
}

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
    condition     = google_container_node_pool.development.node_count == 1 && google_container_node_pool.development.node_config[0].machine_type == "e2-standard-2" && google_container_cluster.development.private_cluster_config[0].enable_private_nodes
    error_message = "Operator wiring must retain the existing single-node private development footprint."
  }
}
