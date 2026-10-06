output "target" { value = "shared-development" }
output "project_id" { value = local.project }
output "region" { value = var.platform_contract.region }
output "cluster" { value = var.platform_contract.cluster.name }
output "location" { value = var.platform_contract.cluster.location }
output "namespace" { value = local.namespace }
output "storage_class" { value = var.platform_contract.storage.class }
output "service_accounts" { value = merge(local.workload_emails, { operator-api = "ec-dev-operator-api@${local.project}.iam.gserviceaccount.com" }) }
output "buckets" { value = merge({ for k, v in google_storage_bucket.data : k => v.name }, { media = google_storage_bucket.media.name }) }
output "image_repository" { value = "${var.platform_contract.region}-docker.pkg.dev/${local.project}/${google_artifact_registry_repository.images.repository_id}" }
output "state_bucket" { value = google_storage_bucket.state.name }
