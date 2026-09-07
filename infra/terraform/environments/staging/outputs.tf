output "cluster" {
  description = "Inputs required by a network-connected deploy runner."
  value = {
    name      = module.gke.cluster_name
    location  = module.gke.cluster_location
    namespace = var.kubernetes_namespace
  }
}

output "container_repository_url" {
  value       = module.artifact_registry.repository_url
  description = "Artifact Registry repository used by release builds."
}

output "cloud_sql_connection_name" {
  value       = module.managed_state.database_connection_name
  description = "Set Helm cloudSqlProxy.instanceConnectionName to this value."
}

output "claim_check_bucket_name" {
  value       = module.storage_kms.claim_check_bucket_name
  description = "Set EC_GCS_CLAIM_CHECK_BUCKET to this value."
}

output "redis_endpoint" {
  value = {
    host = module.managed_state.redis_host
    port = module.managed_state.redis_port
  }
  description = "Private TLS Redis endpoint; authentication material is deliberately excluded."
}

output "kms_key_id" {
  value       = module.storage_kms.kms_key_id
  description = "Envelope encryption key resource ID."
}

output "secret_ids" {
  value       = module.storage_kms.secret_ids
  description = "Secret names for CSI mounts. Populate versions through an audited process."
}

output "workload_service_account_emails" {
  value       = module.workload_identity.workload_service_account_emails
  description = "Map these values to the chart's serviceAccounts annotations."
}

output "redis_server_ca_certificates" {
  value       = module.managed_state.redis_server_ca_certificates
  description = "Public Redis CA material to populate the runtime redis-ca-certificate container."
}
