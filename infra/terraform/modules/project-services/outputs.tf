output "enabled_services" {
  description = "Services managed by this module."
  value       = sort(keys(google_project_service.required))
}
