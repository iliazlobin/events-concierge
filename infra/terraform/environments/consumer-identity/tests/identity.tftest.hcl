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
