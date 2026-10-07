mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789" }
  }
}

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
      "parametermanager.googleapis.com",
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
    operator_iap_member      = "user:maintainer@example.test"
  }
  assert {
    condition     = !google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_signup && google_identity_platform_config.consumer.client[0].permissions[0].disabled_user_deletion
    error_message = "Consumer signup must not bypass the application erasure workflow."
  }
  assert {
    condition     = google_iap_web_backend_service_iam_member.owner[0].member == var.operator_iap_member
    error_message = "The operator edge user must come from the explicit private input."
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

run "existing_foundation_supporting_apis_remain_unowned" {
  command = plan
  variables { foundation_owned_services = ["iam.googleapis.com", "secretmanager.googleapis.com"] }
  assert {
    condition = toset([for api in google_project_service.identity : api.service]) == toset([
      "identitytoolkit.googleapis.com", "apikeys.googleapis.com", "securetoken.googleapis.com",
      "parametermanager.googleapis.com",
    ])
    error_message = "Existing supporting APIs remain in foundation state, while identity APIs stay app-owned."
  }
}

run "parameter_policy_container_without_runtime_access" {
  command = plan
  assert {
    condition     = google_parameter_manager_parameter.operator_rbac.parameter_id == "ec-operator-rbac" && google_parameter_manager_parameter.operator_rbac.format == "JSON" && google_parameter_manager_parameter.operator_rbac.deletion_policy == "PREVENT"
    error_message = "RBAC requires a protected JSON parameter container; identity payloads stay outside Terraform."
  }
  assert {
    condition     = length(google_project_iam_custom_role.operator_policy_reader) == 0 && length(google_project_iam_member.operator_policy_reader) == 0
    error_message = "Runtime policy reads require an explicit dedicated service account."
  }
}

run "parameter_policy_get_only_scoped_to_rbac_versions" {
  command = plan
  variables {
    operator_api_service_account = "operator-api@events-identity-test.iam.gserviceaccount.com"
  }
  assert {
    condition     = google_project_iam_custom_role.operator_policy_reader[0].permissions == toset(["parametermanager.parameterVersions.get"])
    error_message = "The operator API must not list, render secrets, write policy or manage IAM."
  }
  assert {
    condition     = google_project_iam_member.operator_policy_reader[0].member == "serviceAccount:${var.operator_api_service_account}" && google_project_iam_member.operator_policy_reader[0].condition[0].expression == "resource.service == 'parametermanager.googleapis.com' && resource.type == 'parametermanager.googleapis.com/ParameterVersion' && resource.name.startsWith('projects/123456789/locations/global/parameters/ec-operator-rbac/versions/')"
    error_message = "Policy reads must be restricted to this parameter's versions using the numeric project number and resource type."
  }
  assert {
    condition     = output.operator_policy_parameter == "projects/123456789/locations/global/parameters/ec-operator-rbac"
    error_message = "Release configuration needs the canonical numeric project resource."
  }
}

run "edge_without_explicit_member_rejected" {
  command = plan
  variables { operator_backend_service = "operator-backend" }
  expect_failures = [var.operator_iap_member]
}

run "broad_edge_member_rejected" {
  command = plan
  variables {
    operator_backend_service = "operator-backend"
    operator_iap_member      = "allAuthenticatedUsers"
  }
  expect_failures = [var.operator_iap_member]
}

run "external_runtime_identity_rejected" {
  command = plan
  variables { operator_api_service_account = "operator-api@other-project.iam.gserviceaccount.com" }
  expect_failures = [var.operator_api_service_account]
}

run "consumer_policy_access_rejected" {
  command = plan
  variables {
    api_service_account          = "consumer-api@events-identity-test.iam.gserviceaccount.com"
    operator_api_service_account = "consumer-api@events-identity-test.iam.gserviceaccount.com"
  }
  expect_failures = [var.operator_api_service_account]
}

run "eraser_policy_access_rejected" {
  command = plan
  variables {
    erasure_service_account      = "account-eraser@events-identity-test.iam.gserviceaccount.com"
    operator_api_service_account = "account-eraser@events-identity-test.iam.gserviceaccount.com"
  }
  expect_failures = [var.operator_api_service_account]
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
