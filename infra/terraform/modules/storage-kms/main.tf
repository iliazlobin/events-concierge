locals {
  secret_bindings = {
    for binding in flatten([
      for secret_name, members in var.secret_accessors : [
        for member in members : {
          key         = "${secret_name}:${member}"
          secret_name = secret_name
          member      = member
        }
      ]
    ]) : binding.key => binding
  }
}

data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

resource "google_project_service_identity" "secret_manager" {
  provider = google-beta
  project  = var.project_id
  service  = "secretmanager.googleapis.com"
}

resource "google_kms_key_ring" "application" {
  project  = var.project_id
  name     = "${var.name_prefix}-application"
  location = var.region
}

resource "google_kms_crypto_key" "application" {
  name            = "application-envelope"
  key_ring        = google_kms_key_ring.application.id
  rotation_period = "7776000s"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "runtime" {
  for_each = var.runtime_members

  crypto_key_id = google_kms_crypto_key.application.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = each.value
}

resource "google_kms_crypto_key_iam_member" "gcs" {
  crypto_key_id = google_kms_crypto_key.application.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = data.google_storage_project_service_account.gcs.member
}

resource "google_kms_crypto_key_iam_member" "secret_manager" {
  crypto_key_id = google_kms_crypto_key.application.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.secret_manager.email}"
}

resource "google_storage_bucket" "claim_check" {
  project                     = var.project_id
  name                        = var.bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection

  encryption {
    default_kms_key_name = google_kms_crypto_key.application.id
  }

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = var.claim_check_ttl_days
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 7
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    service     = "events-concierge"
    environment = var.name_prefix
    data_class  = "claim-check"
  }

  depends_on = [google_kms_crypto_key_iam_member.gcs]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket_iam_member" "runtime" {
  for_each = var.runtime_members

  bucket = google_storage_bucket.claim_check.name
  role   = "roles/storage.objectAdmin"
  member = each.value
}

resource "google_storage_bucket_iam_member" "catalog_executor" {
  for_each = var.catalog_executor_members

  bucket = google_storage_bucket.claim_check.name
  role   = "roles/storage.objectUser"
  member = each.value

  condition {
    title       = "catalog-claim-check-only"
    description = "Catalog payload access excludes all consumer tenant claim-check objects."
    expression  = "resource.name.startsWith('projects/_/buckets/${var.bucket_name}/objects/${var.catalog_claim_check_prefix}/')"
  }
}

resource "google_secret_manager_secret" "container" {
  for_each = var.secret_names

  project   = var.project_id
  secret_id = "${var.name_prefix}-${each.value}"

  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.application.id
        }
      }
    }
  }

  labels = {
    service     = "events-concierge"
    environment = var.name_prefix
  }

  depends_on = [google_kms_crypto_key_iam_member.secret_manager]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = local.secret_bindings

  project   = var.project_id
  secret_id = google_secret_manager_secret.container[each.value.secret_name].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value.member
}
