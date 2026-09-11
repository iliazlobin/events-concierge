resource "google_project_service" "required" {
  for_each = setunion(var.services, var.additional_services)

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
