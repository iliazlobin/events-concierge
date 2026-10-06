terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    google = { source = "registry.terraform.io/hashicorp/google", version = "7.40.0" }
  }
}

# Use the selected identity project for API quota, preconditions and billing.
provider "google" {
  project                     = var.project_id
  billing_project             = var.project_id
  user_project_override       = true
  impersonate_service_account = var.impersonate_service_account
}

locals {
  hostname    = split(":", split("/", trimprefix(var.public_origin, "https://"))[0])[0]
  auth_domain = "${var.project_id}.firebaseapp.com"
}

# This app-owned state never adopts shared network, cluster or application resources.
resource "google_project_service" "identity" {
  for_each = setsubtract(toset([
    "identitytoolkit.googleapis.com",
    "apikeys.googleapis.com",
    "securetoken.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ]), var.foundation_owned_services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "google_identity_platform_config" "consumer" {
  project            = var.project_id
  authorized_domains = [local.hostname, local.auth_domain]
  sign_in {
    # Never automatically join different provider identities using only their email address.
    allow_duplicate_emails = true
    anonymous { enabled = false }
    email { enabled = false }
    phone_number { enabled = false }
  }
  client {
    permissions {
      disabled_user_signup   = !var.signup_enabled
      disabled_user_deletion = true # Account erasure must finish app and provider cleanup together.
    }
  }
  multi_tenant { allow_tenants = false }
  depends_on = [google_project_service.identity]
  lifecycle { prevent_destroy = true }
}

resource "google_apikeys_key" "browser" {
  project      = var.project_id
  name         = "events-concierge-consumer"
  display_name = "Events Concierge browser authentication"
  restrictions {
    browser_key_restrictions {
      allowed_referrers = [var.public_origin, "${var.public_origin}/*", "https://${local.auth_domain}", "https://${local.auth_domain}/*"]
    }
    api_targets { service = "identitytoolkit.googleapis.com" }
    api_targets { service = "securetoken.googleapis.com" }
  }
  depends_on = [google_project_service.identity]
  lifecycle { prevent_destroy = true }
}

# OAuth provider secrets are read from these versioned containers by the audited bootstrap
# command. The API and coding workers receive no access; secret values never enter this state.
resource "google_secret_manager_secret" "provider" {
  for_each  = toset(["google", "apple"])
  project   = var.project_id
  secret_id = "ec-consumer-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.identity]
  lifecycle { prevent_destroy = true }
}

resource "google_project_iam_custom_role" "reader" {
  project     = var.project_id
  role_id     = "ecConsumerIdentityReader"
  title       = "Events Concierge identity status reader"
  permissions = ["firebaseauth.users.get"]
  depends_on  = [google_project_service.identity]
}
resource "google_project_iam_custom_role" "eraser" {
  project     = var.project_id
  role_id     = "ecConsumerIdentityEraser"
  title       = "Events Concierge managed account eraser"
  permissions = ["firebaseauth.users.get", "firebaseauth.users.delete"]
  depends_on  = [google_project_service.identity]
}
resource "google_project_iam_member" "api_reader" {
  project = var.project_id
  role    = google_project_iam_custom_role.reader.name
  member  = "serviceAccount:${var.api_service_account}"
}
resource "google_project_iam_member" "account_eraser" {
  project = var.project_id
  role    = google_project_iam_custom_role.eraser.name
  member  = "serviceAccount:${var.erasure_service_account}"
}

# Optional binding on the already provisioned operator edge. Consumer sign-in never grants it.
resource "google_iap_web_backend_service_iam_member" "owner" {
  count               = var.operator_backend_service == null ? 0 : 1
  project             = var.project_id
  web_backend_service = var.operator_backend_service
  role                = "roles/iap.httpsResourceAccessor"
  member              = "user:iliazlobin91@gmail.com"
}
