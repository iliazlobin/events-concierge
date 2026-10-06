mock_provider "google" {}

run "only_application_edge_prerequisites" {
  command = plan
  assert {
    condition = (
      google_project_service.certificate_manager.project == "iz27-platform-dev" &&
      google_project_service.certificate_manager.service == "certificatemanager.googleapis.com" &&
      !google_project_service.certificate_manager.disable_on_destroy &&
      google_compute_global_address.public.name == "ec-public-ip" &&
      google_compute_global_address.public.address_type == "EXTERNAL" &&
      google_compute_global_address.public.ip_version == "IPV4"
    )
    error_message = "Only the app certificate API and fixed public IPv4 prerequisite belong here."
  }
  assert {
    condition = (
      toset([for auth in google_certificate_manager_dns_authorization.host : auth.domain]) ==
      toset(["events.iliazlobin.com", "admin-events.iliazlobin.com"]) &&
      alltrue([for auth in google_certificate_manager_dns_authorization.host : auth.type == "PER_PROJECT_RECORD" && auth.location == "global"])
    )
    error_message = "DNS authorization must exclude wildcard domains and unrelated hostnames."
  }
  assert {
    condition = (
      google_certificate_manager_certificate.public.scope == "DEFAULT" &&
      toset(google_certificate_manager_certificate.public.managed[0].domains) ==
      toset(["events.iliazlobin.com", "admin-events.iliazlobin.com"]) &&
      length(google_certificate_manager_certificate.public.self_managed) == 0 &&
      google_certificate_manager_certificate_map.public.name == "ec-public-cert-map"
    )
    error_message = "Use a managed certificate for exactly the two app hostnames, without key material."
  }
  assert {
    condition = (
      length(google_certificate_manager_certificate_map_entry.host) == 2 &&
      toset([for entry in google_certificate_manager_certificate_map_entry.host : entry.hostname]) ==
      toset(["events.iliazlobin.com", "admin-events.iliazlobin.com"]) &&
      alltrue([for entry in google_certificate_manager_certificate_map_entry.host : entry.matcher == null])
    )
    error_message = "No primary/default or wildcard certificate-map match is allowed."
  }
  assert {
    condition = (
      google_compute_ssl_policy.public.profile == "MODERN" &&
      google_compute_ssl_policy.public.min_tls_version == "TLS_1_2" &&
      google_secret_manager_secret.admin_iap.secret_id == "ec-admin-iap"
    )
    error_message = "The browser edge requires TLS 1.2+ and a separate empty admin credential container."
  }
}
