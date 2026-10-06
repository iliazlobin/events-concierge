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
# DNS-only; publish the two A records only after the private release gates pass.
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

output "certificate_console" {
  value = "https://console.cloud.google.com/security/ccm/list/certificates?project=${local.project}&authuser=4"
}
