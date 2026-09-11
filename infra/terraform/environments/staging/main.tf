locals {
  service_accounts = merge({
    frontend = {
      gcp_account_id             = "${var.name_prefix}-frontend"
      kubernetes_service_account = "events-concierge-frontend"
      project_roles              = []
    }
    api = {
      gcp_account_id             = "${var.name_prefix}-api"
      kubernetes_service_account = "events-concierge-api"
      project_roles              = ["roles/cloudsql.client"]
    }
    transactional_worker = {
      gcp_account_id             = "${var.name_prefix}-transactional"
      kubernetes_service_account = "events-concierge-temporal-transactional"
      project_roles              = ["roles/cloudsql.client"]
    }
    catalog_worker = {
      gcp_account_id             = "${var.name_prefix}-catalog-worker"
      kubernetes_service_account = "events-concierge-temporal-catalog"
      project_roles              = ["roles/cloudsql.client"]
    }
    request_starter = {
      gcp_account_id             = "${var.name_prefix}-request-start"
      kubernetes_service_account = "events-concierge-request-starter"
      project_roles              = ["roles/cloudsql.client"]
    }
    notifier = {
      gcp_account_id             = "${var.name_prefix}-notifier"
      kubernetes_service_account = "events-concierge-notifier"
      project_roles              = ["roles/cloudsql.client"]
    }
    account_erasure = {
      gcp_account_id             = "${var.name_prefix}-erasure"
      kubernetes_service_account = "events-concierge-account-erasure"
      project_roles              = ["roles/cloudsql.client"]
    }
    change_delivery = {
      gcp_account_id             = "${var.name_prefix}-change"
      kubernetes_service_account = "events-concierge-change-delivery"
      project_roles              = ["roles/cloudsql.client"]
    }
    handoff_expiry = {
      gcp_account_id             = "${var.name_prefix}-handoff"
      kubernetes_service_account = "events-concierge-handoff-expiry"
      project_roles              = ["roles/cloudsql.client"]
    }
    lifecycle_invariants = {
      gcp_account_id             = "${var.name_prefix}-invariants"
      kubernetes_service_account = "events-concierge-lifecycle-invariants"
      project_roles              = ["roles/cloudsql.client"]
    }
    catalog_jobs = {
      gcp_account_id             = "${var.name_prefix}-catalog"
      kubernetes_service_account = "events-concierge-catalog-jobs"
      project_roles              = ["roles/cloudsql.client"]
    }
    migration = {
      gcp_account_id             = "${var.name_prefix}-migration"
      kubernetes_service_account = "events-concierge-migration"
      project_roles              = ["roles/cloudsql.client"]
    }
    }, var.operator_enabled ? {
    operator_api = {
      gcp_account_id             = "${var.name_prefix}-operator"
      kubernetes_service_account = "events-concierge-operator-api"
      project_roles              = ["roles/cloudsql.client"]
    }
    ingestion_executor = {
      gcp_account_id             = "${var.name_prefix}-executor"
      kubernetes_service_account = "events-concierge-ingestion-executor"
      project_roles              = ["roles/cloudsql.client"]
    }
  } : {})

  runtime_keys = toset([
    "api",
    "transactional_worker",
    "catalog_worker",
    "request_starter",
    "notifier",
    "account_erasure",
    "change_delivery",
    "handoff_expiry",
    "lifecycle_invariants",
    "catalog_jobs",
  ])

  runtime_secret_names = toset([
    "database-url",
    "email-provider-token",
    "oidc-client-secret",
    "redis-ca-certificate",
    "redis-url",
    "temporal-api-key",
  ])

  migration_secret_names = toset([
    "app-role-password",
    "migration-url",
  ])

  operator_secret_names = var.operator_enabled ? toset([
    "operator-database-url",
    "ingestion-executor-database-url",
  ]) : toset([])
  executor_shared_secret_names = toset(["redis-url", "redis-ca-certificate", "temporal-api-key"])
  all_secret_names = setunion(
    local.runtime_secret_names, local.migration_secret_names, local.operator_secret_names,
  )
  # IAM resource instance keys must be known during a fresh plan, before the accounts exist.
  # These addresses are deterministic from the same inputs passed to workload_identity.
  workload_iam_members = {
    for key, account in local.service_accounts :
    key => "serviceAccount:${account.gcp_account_id}@${var.project_id}.iam.gserviceaccount.com"
  }
  runtime_members = toset([
    for key in local.runtime_keys : local.workload_iam_members[key]
  ])
  migration_members = toset([
    local.workload_iam_members["migration"],
  ])
  operator_members = var.operator_enabled ? toset([
    local.workload_iam_members["operator_api"],
  ]) : toset([])
  executor_members = var.operator_enabled ? toset([
    local.workload_iam_members["ingestion_executor"],
  ]) : toset([])
  secret_accessors = merge(
    { for name in local.runtime_secret_names : name => setunion(
      local.runtime_members,
      contains(local.executor_shared_secret_names, name) ? local.executor_members : toset([]),
    ) },
    { for name in local.migration_secret_names : name => local.migration_members },
    var.operator_enabled ? {
      "operator-database-url"           = local.operator_members
      "ingestion-executor-database-url" = local.executor_members
    } : {},
  )
}

module "project_services" {
  source = "../../modules/project-services"

  project_id          = var.project_id
  additional_services = var.operator_enabled ? ["iap.googleapis.com"] : []
}

module "network" {
  source = "../../modules/network"

  project_id    = var.project_id
  name_prefix   = var.name_prefix
  region        = var.region
  subnet_cidr   = var.subnet_cidr
  pods_cidr     = var.pods_cidr
  services_cidr = var.services_cidr

  depends_on = [module.project_services]
}

module "workload_identity" {
  source = "../../modules/workload-identity"

  project_id           = var.project_id
  name_prefix          = var.name_prefix
  kubernetes_namespace = var.kubernetes_namespace
  service_accounts     = local.service_accounts

  depends_on = [module.project_services]
}

module "gke" {
  source = "../../modules/gke"

  project_id                 = var.project_id
  name_prefix                = var.name_prefix
  location                   = var.control_plane_zone
  node_locations             = var.node_locations
  network_id                 = module.network.network_id
  subnetwork_id              = module.network.subnetwork_id
  pods_range_name            = module.network.pods_range_name
  services_range_name        = module.network.services_range_name
  node_service_account_email = module.workload_identity.node_service_account_email
  machine_type               = var.gke_machine_type
  max_nodes_per_zone         = var.gke_max_nodes_per_zone
  master_ipv4_cidr           = var.master_ipv4_cidr
  enable_private_endpoint    = var.enable_private_endpoint
  master_authorized_networks = var.master_authorized_networks
  deletion_protection        = var.deletion_protection
}

module "managed_state" {
  source = "../../modules/managed-state"

  project_id           = var.project_id
  name_prefix          = var.name_prefix
  region               = var.region
  network_id           = module.network.network_id
  database_tier        = var.database_tier
  redis_memory_size_gb = var.redis_memory_size_gb
  deletion_protection  = var.deletion_protection

  depends_on = [module.network]
}

module "storage_kms" {
  source = "../../modules/storage-kms"

  project_id                 = var.project_id
  name_prefix                = var.name_prefix
  region                     = var.region
  bucket_name                = var.claim_check_bucket_name
  runtime_members            = local.runtime_members
  catalog_executor_members   = local.executor_members
  catalog_claim_check_prefix = var.catalog_claim_check_prefix
  secret_names               = local.all_secret_names
  secret_accessors           = local.secret_accessors
  deletion_protection        = var.deletion_protection

  depends_on = [module.project_services, module.workload_identity]
}

module "artifact_registry" {
  source = "../../modules/artifact-registry"

  project_id     = var.project_id
  name_prefix    = var.name_prefix
  region         = var.region
  reader_members = ["serviceAccount:${var.name_prefix}-gke-node@${var.project_id}.iam.gserviceaccount.com"]

  depends_on = [module.project_services, module.workload_identity]
}

module "observability" {
  source = "../../modules/observability"

  project_id              = var.project_id
  name_prefix             = var.name_prefix
  notification_email      = var.notification_email
  cloud_sql_instance_name = module.managed_state.database_instance_name
  redis_instance_name     = "${var.name_prefix}-redis"
  gke_cluster_name        = module.gke.cluster_name

  depends_on = [module.project_services]
}
