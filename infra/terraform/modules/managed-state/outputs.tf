output "database_instance_name" {
  value       = google_sql_database_instance.application.name
  description = "Cloud SQL instance name."
}

output "database_connection_name" {
  value       = google_sql_database_instance.application.connection_name
  description = "Cloud SQL Auth Proxy connection name."
}

output "database_name" {
  value       = google_sql_database.application.name
  description = "Application database name."
}

output "redis_host" {
  value       = google_redis_instance.application.host
  description = "Private Redis endpoint; no authentication material is exported."
}

output "redis_port" {
  value       = google_redis_instance.application.port
  description = "TLS Redis port."
}

output "redis_server_ca_certificates" {
  value       = google_redis_instance.application.server_ca_certs
  description = "Redis server CA certificates suitable for a trusted file mount."
}
