output "claim_check_bucket_name" {
  value       = google_storage_bucket.claim_check.name
  description = "Regional GCS claim-check bucket."
}

output "kms_key_id" {
  value       = google_kms_crypto_key.application.id
  description = "Envelope/CMEK key resource ID."
}

output "secret_resource_ids" {
  value       = { for key, secret in google_secret_manager_secret.container : key => secret.id }
  description = "Secret container IDs; secret version data is never managed by this stack."
}

output "secret_ids" {
  value       = { for key, secret in google_secret_manager_secret.container : key => secret.secret_id }
  description = "Environment-prefixed Secret Manager IDs for Helm values."
}
