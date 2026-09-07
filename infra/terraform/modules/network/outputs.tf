output "network_id" {
  value       = google_compute_network.application.id
  description = "VPC resource ID."
}

output "network_name" {
  value       = google_compute_network.application.name
  description = "VPC name."
}

output "subnetwork_id" {
  value       = google_compute_subnetwork.application.id
  description = "GKE subnetwork resource ID."
}

output "pods_range_name" {
  value       = google_compute_subnetwork.application.secondary_ip_range[0].range_name
  description = "Named GKE Pod secondary range."
}

output "services_range_name" {
  value       = google_compute_subnetwork.application.secondary_ip_range[1].range_name
  description = "Named GKE Service secondary range."
}

output "private_service_connection" {
  value       = google_service_networking_connection.private_service_access.id
  description = "Private Service Access connection used by Cloud SQL and Redis."
}
