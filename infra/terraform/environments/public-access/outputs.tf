output "gateway_prerequisites" {
  value = {
    project         = local.project
    static_ip_name  = google_compute_global_address.public.name
    static_ip       = google_compute_global_address.public.address
    certificate_map = google_certificate_manager_certificate_map.public.name
    ssl_policy      = google_compute_ssl_policy.public.name
    admin_secret_id = google_secret_manager_secret.admin_iap.secret_id
  }
}

# DNS authorization does not publish an application route. Keep these CNAMEs
# DNS-only; publish the A record only after the private release gates pass.
output "certificate_dns_records" {
  value = { for key, auth in google_certificate_manager_dns_authorization.host : key => {
    name = auth.dns_resource_record[0].name
    type = auth.dns_resource_record[0].type
    data = auth.dns_resource_record[0].data
  } }
}

output "publication_dns_records" {
  value = { for key, host in local.hosts : key => {
    name = "${host}."
    type = "A"
    data = google_compute_global_address.public.address
  } }
}

# Google assigns these servers. Add this NS RRset only in the existing parent
# authority, after verifying both child records; do not change registrar servers.
output "cloud_dns_delegation" {
  value = var.enable_cloud_dns ? {
    zone_name = google_dns_managed_zone.events[0].name
    dns_name  = google_dns_managed_zone.events[0].dns_name
    parent_ns_record = {
      name = google_dns_managed_zone.events[0].dns_name
      type = "NS"
      ttl  = 300
      data = google_dns_managed_zone.events[0].name_servers
    }
  } : null
}

output "certificate_console" {
  value = "https://console.cloud.google.com/security/ccm/list/certificates?project=${local.project}&authuser=4"
}
