output "repository_id" {
  value       = google_artifact_registry_repository.application.repository_id
  description = "Artifact Registry repository ID."
}

output "repository_url" {
  value       = "${google_artifact_registry_repository.application.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.application.repository_id}"
  description = "Repository base URL, without an image tag or digest."
}
