# Cloud DNS API enablement remains a shared-platform prerequisite. This root
# manages only the optional app child zone; the parent DNS authority delegates it.
resource "google_dns_managed_zone" "events" {
  count         = var.enable_cloud_dns ? 1 : 0
  project       = local.project
  name          = "ec-events-public"
  dns_name      = "${local.hosts.consumer}."
  description   = "Public DNS for Events Concierge only."
  visibility    = "public"
  force_destroy = false
  dnssec_config { state = "off" }
  lifecycle { prevent_destroy = true }
}

resource "google_dns_record_set" "certificate_validation" {
  for_each     = var.enable_cloud_dns ? google_certificate_manager_dns_authorization.host : {}
  project      = local.project
  managed_zone = google_dns_managed_zone.events[0].name
  name         = each.value.dns_resource_record[0].name
  type         = each.value.dns_resource_record[0].type
  ttl          = 300
  rrdatas      = [each.value.dns_resource_record[0].data]
  lifecycle { prevent_destroy = true }
}

resource "google_dns_record_set" "publication" {
  for_each     = var.enable_cloud_dns ? local.hosts : {}
  project      = local.project
  managed_zone = google_dns_managed_zone.events[0].name
  name         = "${each.value}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.public.address]
  lifecycle { prevent_destroy = true }
}
