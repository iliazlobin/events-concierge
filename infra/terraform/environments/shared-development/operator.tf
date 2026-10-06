# Dedicated controller: it never joins local.names or consumer/storage grant sets.
resource "google_service_account" "operator_api" {
  project      = local.project
  account_id   = "ec-dev-operator-api"
  display_name = "Events Concierge private operator API"
}

resource "google_service_account_iam_member" "operator_api_workload" {
  service_account_id = "projects/${local.project}/serviceAccounts/ec-dev-operator-api@${local.project}.iam.gserviceaccount.com"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.project}.svc.id.goog[${local.namespace}/events-concierge-operator-api]"
  depends_on         = [google_service_account.operator_api]
}

resource "google_secret_manager_secret_iam_member" "operator_api_database" {
  project    = local.project
  secret_id  = google_secret_manager_secret.development["operator-database-url"].secret_id
  role       = "roles/secretmanager.secretAccessor"
  member     = "serviceAccount:ec-dev-operator-api@${local.project}.iam.gserviceaccount.com"
  depends_on = [google_service_account.operator_api]
}
