# Recovery copies stay outside workload grant sets. Upload versions through the
# authorized custody procedure; Terraform must never receive certificate keys.
resource "google_secret_manager_secret" "tls_recovery" {
  for_each  = toset(["postgres", "redis", "temporal"])
  project   = local.project
  secret_id = "ec-dev-${each.key}-ca-recovery-g1"
  labels = {
    purpose    = "ca-recovery"
    generation = "1"
  }
  replication {
    auto {}
  }
  version_destroy_ttl = "2592000s"
  lifecycle { prevent_destroy = true }
}
