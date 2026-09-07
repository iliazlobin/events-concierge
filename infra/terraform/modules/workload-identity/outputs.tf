output "node_service_account_email" {
  value       = google_service_account.node.email
  description = "GKE node service account."
}

output "workload_service_account_emails" {
  value       = { for key, account in google_service_account.workload : key => account.email }
  description = "Google service account email by workload key."
}

output "workload_service_account_members" {
  value       = { for key, account in google_service_account.workload : key => account.member }
  description = "IAM member string by workload key."
}
