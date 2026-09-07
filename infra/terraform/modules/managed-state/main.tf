resource "google_sql_database_instance" "application" {
  project             = var.project_id
  name                = "${var.name_prefix}-postgres"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier                        = var.database_tier
    availability_type           = "REGIONAL"
    deletion_protection_enabled = var.deletion_protection
    disk_type                   = "PD_SSD"
    disk_size                   = var.database_disk_size_gb
    disk_autoresize             = true

    backup_configuration {
      enabled                        = true
      start_time                     = "09:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 14
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = var.network_id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    insights_config {
      query_insights_enabled  = true
      query_string_length     = 1024
      record_application_tags = true
      record_client_address   = false
    }

    maintenance_window {
      day          = 7
      hour         = 10
      update_track = "stable"
    }

    user_labels = {
      service     = "events-concierge"
      environment = var.name_prefix
    }
  }
}

resource "google_sql_database" "application" {
  project   = var.project_id
  name      = var.database_name
  instance  = google_sql_database_instance.application.name
  charset   = "UTF8"
  collation = "en_US.UTF8"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_redis_instance" "application" {
  project                 = var.project_id
  name                    = "${var.name_prefix}-redis"
  display_name            = "Events Concierge ${var.name_prefix}"
  region                  = var.region
  tier                    = "STANDARD_HA"
  memory_size_gb          = var.redis_memory_size_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = var.network_id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  auth_enabled            = true

  redis_configs = {
    maxmemory-policy = "noeviction"
  }

  persistence_config {
    persistence_mode    = "RDB"
    rdb_snapshot_period = "ONE_HOUR"
  }

  labels = {
    service     = "events-concierge"
    environment = var.name_prefix
  }

  lifecycle {
    prevent_destroy = true
  }
}
