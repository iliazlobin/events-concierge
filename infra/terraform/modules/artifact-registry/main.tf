resource "google_artifact_registry_repository" "application" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.name_prefix}-containers"
  description   = "Immutable Events Concierge application images"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = "2592000s"
    }
  }

  cleanup_policies {
    id     = "retain-recent-versions"
    action = "KEEP"

    most_recent_versions {
      keep_count = 20
    }
  }

  labels = {
    service     = "events-concierge"
    environment = var.name_prefix
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_artifact_registry_repository_iam_member" "reader" {
  for_each = var.reader_members

  project    = var.project_id
  location   = google_artifact_registry_repository.application.location
  repository = google_artifact_registry_repository.application.name
  role       = "roles/artifactregistry.reader"
  member     = each.value
}
