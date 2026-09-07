resource "google_container_cluster" "application" {
  project  = var.project_id
  name     = "${var.name_prefix}-gke"
  location = var.location

  node_locations           = var.node_locations
  network                  = var.network_id
  subnetwork               = var.subnetwork_id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  enable_shielded_nodes    = true
  networking_mode          = "VPC_NATIVE"
  datapath_provider        = "ADVANCED_DATAPATH"

  release_channel {
    channel = "STABLE"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.master_ipv4_cidr
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_networks) == 0 ? [] : [1]
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_networks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  secret_manager_config {
    enabled = true
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER", "STORAGE", "POD", "DEPLOYMENT", "STATEFULSET", "DAEMONSET", "HPA"]

    managed_prometheus {
      enabled = true
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]
  }

  addons_config {
    http_load_balancing {
      disabled = false
    }
  }

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  resource_labels = {
    service     = "events-concierge"
    environment = var.name_prefix
  }

  lifecycle {
    precondition {
      condition     = var.enable_private_endpoint || length(var.master_authorized_networks) > 0
      error_message = "A public GKE control-plane endpoint requires at least one narrow authorized network."
    }
  }
}

resource "google_container_node_pool" "application" {
  project        = var.project_id
  name           = "${var.name_prefix}-application"
  location       = var.location
  cluster        = google_container_cluster.application.name
  node_locations = var.node_locations

  autoscaling {
    min_node_count = var.min_nodes_per_zone
    max_node_count = var.max_nodes_per_zone
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    strategy        = "SURGE"
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 100
    image_type      = "COS_CONTAINERD"
    service_account = var.node_service_account_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      service     = "events-concierge"
      environment = var.name_prefix
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_integrity_monitoring = true
      enable_secure_boot          = true
    }
  }
}
