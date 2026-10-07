mock_provider "google" {
  override_during = plan
  mock_resource "google_compute_global_address" {
    defaults = { address = "192.0.2.42" }
  }
  mock_resource "google_certificate_manager_dns_authorization" {
    defaults = {
      dns_resource_record = [{
        name = "_acme-challenge_project.events.iliazlobin.com."
        type = "CNAME"
        data = "project.authorize.certificatemanager.goog."
      }]
    }
  }
  mock_resource "google_dns_managed_zone" {
    defaults = { name_servers = ["ns-cloud-a1.googledomains.com.", "ns-cloud-a2.googledomains.com."] }
  }
}

run "existing_dns_ownership_is_default" {
  command = plan
  assert {
    condition = (
      length(google_dns_managed_zone.events) == 0 &&
      length(google_dns_record_set.publication) == 0 &&
      length(google_dns_record_set.certificate_validation) == 0 &&
      output.cloud_dns_delegation == null
    )
    error_message = "The default must not adopt existing DNS or change its authority."
  }
}

run "only_events_child_zone_and_existing_records" {
  command = plan
  variables { enable_cloud_dns = true }
  assert {
    condition = (
      length(google_dns_managed_zone.events) == 1 &&
      google_dns_managed_zone.events[0].project == "iz27-platform-dev" &&
      google_dns_managed_zone.events[0].name == "ec-events-public" &&
      google_dns_managed_zone.events[0].dns_name == "events.iliazlobin.com." &&
      google_dns_managed_zone.events[0].visibility == "public" &&
      google_dns_managed_zone.events[0].dnssec_config[0].state == "off" &&
      !google_dns_managed_zone.events[0].force_destroy
    )
    error_message = "Create only the protected unsigned public child zone, never the apex or a private zone."
  }
  assert {
    condition = (
      length(google_dns_record_set.publication) == 1 &&
      google_dns_record_set.publication["consumer"].project == "iz27-platform-dev" &&
      google_dns_record_set.publication["consumer"].managed_zone == google_dns_managed_zone.events[0].name &&
      google_dns_record_set.publication["consumer"].name == "events.iliazlobin.com." &&
      google_dns_record_set.publication["consumer"].type == "A" &&
      google_dns_record_set.publication["consumer"].ttl == 300 &&
      google_dns_record_set.publication["consumer"].rrdatas == tolist([google_compute_global_address.public.address])
    )
    error_message = "The child apex A must use the existing reserved address, without another hostname or record."
  }
  assert {
    condition = (
      length(google_dns_record_set.certificate_validation) == 1 &&
      google_dns_record_set.certificate_validation["consumer"].project == "iz27-platform-dev" &&
      google_dns_record_set.certificate_validation["consumer"].managed_zone == google_dns_managed_zone.events[0].name &&
      google_dns_record_set.certificate_validation["consumer"].name == "_acme-challenge_project.events.iliazlobin.com." &&
      google_dns_record_set.certificate_validation["consumer"].type == "CNAME" &&
      google_dns_record_set.certificate_validation["consumer"].ttl == 300 &&
      google_dns_record_set.certificate_validation["consumer"].rrdatas == tolist(["project.authorize.certificatemanager.goog."])
    )
    error_message = "Copy only the existing per-project authorization CNAME, including its exact generated name and target."
  }
  assert {
    condition = (
      output.cloud_dns_delegation.zone_name == "ec-events-public" &&
      output.cloud_dns_delegation.dns_name == "events.iliazlobin.com." &&
      output.cloud_dns_delegation.parent_ns_record.name == "events.iliazlobin.com." &&
      output.cloud_dns_delegation.parent_ns_record.type == "NS" &&
      output.cloud_dns_delegation.parent_ns_record.ttl == 300 &&
      output.cloud_dns_delegation.parent_ns_record.data == google_dns_managed_zone.events[0].name_servers
    )
    error_message = "Delegation must report Google's assigned child name servers; it must not manage the parent or registrar."
  }
  assert {
    condition = (
      google_compute_global_address.public.name == "ec-public-ip" &&
      google_certificate_manager_dns_authorization.host["consumer"].domain == "events.iliazlobin.com" &&
      google_certificate_manager_dns_authorization.host["consumer"].type == "PER_PROJECT_RECORD" &&
      google_certificate_manager_certificate.public.location == "global" &&
      google_certificate_manager_certificate_map.public.name == "ec-public-cert-map" &&
      google_compute_ssl_policy.public.profile == "MODERN" &&
      google_compute_ssl_policy.public.min_tls_version == "TLS_1_2" &&
      google_secret_manager_secret.admin_iap.secret_id == "ec-admin-iap"
    )
    error_message = "DNS delegation must retain the existing certificate, IP, TLS policy and credential-container configuration."
  }
}

run "provider_outputs_are_preserved_exactly" {
  command = plan
  variables { enable_cloud_dns = true }
  override_resource {
    target          = google_compute_global_address.public
    override_during = plan
    values          = { address = "192.0.2.99" }
  }
  override_resource {
    target          = google_certificate_manager_dns_authorization.host["consumer"]
    override_during = plan
    values = {
      dns_resource_record = [{
        name = "_acme-challenge_another-project.events.iliazlobin.com."
        type = "CNAME"
        data = "another-project.authorize.certificatemanager.goog."
      }]
    }
  }
  override_resource {
    target          = google_dns_managed_zone.events[0]
    override_during = plan
    values          = { name_servers = ["ns-cloud-b1.googledomains.com.", "ns-cloud-b2.googledomains.com."] }
  }
  assert {
    condition = (
      google_dns_record_set.publication["consumer"].rrdatas == tolist(["192.0.2.99"]) &&
      google_dns_record_set.certificate_validation["consumer"].name == "_acme-challenge_another-project.events.iliazlobin.com." &&
      google_dns_record_set.certificate_validation["consumer"].rrdatas == tolist(["another-project.authorize.certificatemanager.goog."]) &&
      output.cloud_dns_delegation.parent_ns_record.data == tolist(["ns-cloud-b1.googledomains.com.", "ns-cloud-b2.googledomains.com."])
    )
    error_message = "Preserve assigned IP, per-project validation prefix/target and assigned servers; do not reconstruct or hardcode them."
  }
}
