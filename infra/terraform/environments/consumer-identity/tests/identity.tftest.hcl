mock_provider "google" {}

variables {
  project_id              = "events-identity-test"
  public_origin           = "https://events.example.test"
  api_service_account     = "api@events-identity-test.iam.gserviceaccount.com"
  erasure_service_account = "eraser@events-identity-test.iam.gserviceaccount.com"
}

run "closed_by_default" {
  command = plan
  assert {
    condition = toset([for api in google_project_service.identity : api.service]) == toset([
      "identitytoolkit.googleapis.com",
      "apikeys.googleapis.com",
      "securetoken.googleapis.com",
      "secretmanager.googleapis.com",
      "iam.googleapis.com",
    ]) && alltrue([for api in google_project_service.identity : !api.disable_on_destroy])
    error_message = "Identity, provider secret containers and custom roles require their APIs; state removal must leave the APIs enabled."
  }
  assert {
    condition     = google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_signup && google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_deletion
    error_message = "Signup starts disabled; only the erasure worker may delete managed accounts."
  }
  assert {
    condition     = google_identity_platform_config.consumer.sign_in[0].allow_duplicate_emails && !google_identity_platform_config.consumer.sign_in[0].anonymous[0].enabled && !google_identity_platform_config.consumer.sign_in[0].email[0].enabled && !google_identity_platform_config.consumer.sign_in[0].phone_number[0].enabled
    error_message = "Guest browsing creates no identity; email/password/phone signup and email account merging are disabled."
  }
  assert {
    condition     = google_project_iam_custom_role.reader.permissions == toset(["firebaseauth.users.get"]) && google_project_iam_custom_role.eraser.permissions == toset(["firebaseauth.users.get", "firebaseauth.users.delete"])
    error_message = "The API only reads status; identity deletion belongs to the account erasure worker."
  }
}

run "explicit_activation_and_owner_edge" {
  command = plan
  variables {
    signup_enabled           = true
    operator_backend_service = "operator-backend"
  }
  assert {
    condition     = !google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_signup && google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_deletion
    error_message = "Consumer signup must not bypass the application erasure workflow."
  }
  assert {
    condition     = google_iap_web_backend_service_iam_member.owner[0].member == "user:iliazlobin91@gmail.com"
    error_message = "The only configured operator edge user is the owner."
  }
}

run "public_google_origin" {
  command = plan
  variables { public_origin = "https://events.iliazlobin.com" }
  assert {
    condition     = toset(google_identity_platform_config.consumer.authorized_domains) == toset(["events.iliazlobin.com", "events-identity-test.firebaseapp.com"])
    error_message = "Only the approved application and project auth hostnames are authorized."
  }
  assert {
    condition     = toset(google_apikeys_key.browser.restrictions[0].browser_key_restrictions[0].allowed_referrers) == toset(["https://events.iliazlobin.com", "https://events.iliazlobin.com/*", "https://events-identity-test.firebaseapp.com", "https://events-identity-test.firebaseapp.com/*"])
    error_message = "The browser key must exclude wildcard subdomains, other ports and unrelated apps."
  }
}

run "loopback_rejected" {
  command = plan
  variables { public_origin = "https://localhost:14443" }
  expect_failures = [var.public_origin]
}

run "wildcard_rejected" {
  command = plan
  variables { public_origin = "https://*.iliazlobin.com" }
  expect_failures = [var.public_origin]
}
