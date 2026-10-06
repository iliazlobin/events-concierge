terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    google = { source = "registry.terraform.io/hashicorp/google", version = "7.40.0" }
  }
}

provider "google" {
  project                     = local.project
  billing_project             = local.project
  user_project_override       = true
  impersonate_service_account = var.impersonate_service_account
}

locals {
  project = "iz27-platform-dev"
  hosts = {
    consumer = "events.iliazlobin.com"
  }
}

# Compute, IAP and Secret Manager APIs remain in the foundation state.
# Gateway reconciliation owns the load balancer and NEGs; this root owns only
# its persistent address, certificate prerequisites and empty credential container.
resource "google_project_service" "certificate_manager" {
  project            = local.project
  service            = "certificatemanager.googleapis.com"
  disable_on_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "google_compute_global_address" "public" {
  project      = local.project
  name         = "ec-public-ip"
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
  lifecycle { prevent_destroy = true }
}

resource "google_certificate_manager_dns_authorization" "host" {
  for_each   = local.hosts
  project    = local.project
  name       = "ec-public-${each.key}"
  location   = "global"
  domain     = each.value
  type       = "PER_PROJECT_RECORD"
  depends_on = [google_project_service.certificate_manager]
  lifecycle { prevent_destroy = true }
}

resource "google_certificate_manager_certificate" "public" {
  project  = local.project
  name     = "ec-public-certificate"
  location = "global"
  scope    = "DEFAULT"
  managed {
    domains            = sort(values(local.hosts))
    dns_authorizations = [for auth in google_certificate_manager_dns_authorization.host : auth.id]
  }
  lifecycle { prevent_destroy = true }
}

resource "google_certificate_manager_certificate_map" "public" {
  project    = local.project
  name       = "ec-public-cert-map"
  depends_on = [google_project_service.certificate_manager]
  lifecycle { prevent_destroy = true }
}

resource "google_certificate_manager_certificate_map_entry" "host" {
  for_each     = local.hosts
  project      = local.project
  name         = "ec-public-${each.key}"
  map          = google_certificate_manager_certificate_map.public.name
  hostname     = each.value
  certificates = [google_certificate_manager_certificate.public.id]
  lifecycle { prevent_destroy = true }
}

resource "google_compute_ssl_policy" "public" {
  project         = local.project
  name            = "ec-public-tls"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
  lifecycle { prevent_destroy = true }
}

# The separate External IAP OAuth client is supplied through an approved import,
# never a Terraform secret version or a Helm value. No workload IAM grant is added.
resource "google_secret_manager_secret" "admin_iap" {
  project   = local.project
  secret_id = "ec-admin-iap"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}
