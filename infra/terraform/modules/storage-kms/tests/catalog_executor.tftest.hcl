# No real provider operations; verify the policy rendered for the executor's catalog objects.
mock_provider "google" {
  mock_resource "google_kms_key_ring" {
    defaults = {
      id = "projects/events-validation/locations/us-west1/keyRings/validation"
    }
  }
  mock_resource "google_kms_crypto_key" {
    defaults = {
      id = "projects/events-validation/locations/us-west1/keyRings/validation/cryptoKeys/validation"
    }
  }
  mock_data "google_storage_project_service_account" {
    defaults = {
      member = "serviceAccount:service-123@gs-project-accounts.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}

variables {
  project_id                 = "events-validation"
  name_prefix                = "ec-test"
  region                     = "us-west1"
  bucket_name                = "events-validation-claim-check"
  secret_names               = []
  catalog_executor_members   = ["serviceAccount:executor@events-validation.iam.gserviceaccount.com"]
  catalog_claim_check_prefix = "events-concierge/catalog/claim-check/v1"
}

run "catalog_prefix_only" {
  command = plan

  assert {
    condition     = length(google_storage_bucket_iam_member.catalog_executor) == 1 && length(google_storage_bucket_iam_member.runtime) == 0 && length(google_kms_crypto_key_iam_member.runtime) == 0
    error_message = "Catalog-only identities cannot receive whole-bucket or envelope-key grants."
  }
  assert {
    condition     = one(values(google_storage_bucket_iam_member.catalog_executor)).role == "roles/storage.objectUser"
    error_message = "Catalog storage grant must remain an object role on one bucket."
  }
  assert {
    condition     = one(one(values(google_storage_bucket_iam_member.catalog_executor)).condition).expression == "resource.name.startsWith('projects/_/buckets/events-validation-claim-check/objects/events-concierge/catalog/claim-check/v1/')"
    error_message = "Object IAM must include the exact catalog prefix and trailing slash, excluding consumer data."
  }
}

run "reject_consumer_prefix" {
  command = plan
  variables {
    catalog_claim_check_prefix = "events-concierge/claim-check/v1"
  }
  expect_failures = [var.catalog_claim_check_prefix]
}
