locals {
  notification_channels = var.notification_email == "" ? [] : [google_monitoring_notification_channel.email[0].name]
}

resource "google_monitoring_notification_channel" "email" {
  count = var.notification_email == "" ? 0 : 1

  project      = var.project_id
  display_name = "${var.name_prefix} operations email"
  type         = "email"

  labels = {
    email_address = var.notification_email
  }
}

resource "google_monitoring_alert_policy" "cloud_sql_cpu" {
  project               = var.project_id
  display_name          = "${var.name_prefix}: Cloud SQL CPU saturation"
  combiner              = "OR"
  notification_channels = local.notification_channels

  conditions {
    display_name = "Cloud SQL CPU above 80% for 10 minutes"

    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\" AND resource.label.database_id = \"${var.project_id}:${var.cloud_sql_instance_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "Check query load, pool budgets, and instance capacity before scaling application replicas."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "redis_memory" {
  project               = var.project_id
  display_name          = "${var.name_prefix}: Redis memory pressure"
  combiner              = "OR"
  notification_channels = local.notification_channels

  conditions {
    display_name = "Redis memory usage ratio above 80% for 10 minutes"

    condition_threshold {
      filter          = "resource.type = \"redis_instance\" AND metric.type = \"redis.googleapis.com/stats/memory/usage_ratio\" AND resource.label.instance_id = \"${var.redis_instance_name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "The instance is configured noeviction; investigate pacing/session growth before availability is affected."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_dashboard" "operations" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "${var.name_prefix} Events Concierge"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width  = 6
          height = 4
          widget = {
            title = "Cloud SQL CPU"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloudsql_database\" metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" resource.label.database_id=\"${var.project_id}:${var.cloud_sql_instance_name}\""
                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
              }]
              yAxis = { label = "utilization", scale = "LINEAR" }
            }
          }
        },
        {
          width  = 6
          height = 4
          xPos   = 6
          widget = {
            title = "Redis memory usage"
            xyChart = {
              dataSets = [{
                plotType   = "LINE"
                targetAxis = "Y1"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"redis_instance\" metric.type=\"redis.googleapis.com/stats/memory/usage_ratio\" resource.label.instance_id=\"${var.redis_instance_name}\""
                    aggregation = {
                      alignmentPeriod  = "60s"
                      perSeriesAligner = "ALIGN_MEAN"
                    }
                  }
                }
              }]
              yAxis = { label = "ratio", scale = "LINEAR" }
            }
          }
        },
        {
          width  = 12
          height = 4
          yPos   = 4
          widget = {
            title = "GKE container restarts"
            xyChart = {
              dataSets = [{
                plotType   = "STACKED_BAR"
                targetAxis = "Y1"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"k8s_container\" metric.type=\"kubernetes.io/container/restart_count\" resource.label.cluster_name=\"${var.gke_cluster_name}\""
                    aggregation = {
                      alignmentPeriod  = "300s"
                      perSeriesAligner = "ALIGN_DELTA"
                      groupByFields    = ["resource.label.container_name"]
                    }
                  }
                }
              }]
              yAxis = { label = "restarts", scale = "LINEAR" }
            }
          }
        }
      ]
    }
  })
}
