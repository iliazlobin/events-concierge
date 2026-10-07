output "identity_project" { value = var.project_id }
output "auth_domain" { value = local.auth_domain }
output "browser_api_key" {
  value     = google_apikeys_key.browser.key_string
  sensitive = true # Public client identifier; still avoid printing it into build logs.
}
output "provider_secret_ids" {
  value = { for provider, secret in google_secret_manager_secret.provider : provider => secret.secret_id }
}
output "identity_console" {
  value = "https://console.cloud.google.com/customer-identity/providers?project=${var.project_id}&authuser=4"
}
output "operator_policy_parameter" {
  value       = local.operator_policy_parameter
  description = "Create a private RBAC JSON version outside Terraform and pin its explicit version resource in Helm."
}
