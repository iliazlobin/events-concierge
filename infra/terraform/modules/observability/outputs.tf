output "dashboard_id" {
  value       = google_monitoring_dashboard.operations.id
  description = "Operations dashboard resource ID."
}
