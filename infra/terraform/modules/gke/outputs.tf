output "cluster_name" {
  value       = google_container_cluster.application.name
  description = "GKE cluster name."
}

output "cluster_location" {
  value       = google_container_cluster.application.location
  description = "GKE control-plane location."
}

output "workload_pool" {
  value       = google_container_cluster.application.workload_identity_config[0].workload_pool
  description = "Workload Identity pool configured on the cluster."
}
