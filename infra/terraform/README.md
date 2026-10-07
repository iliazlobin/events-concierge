# GCP application infrastructure

## Ownership and roots

Each root has separate protected state. Shared platform resources stay in
[gcp-foundation](https://github.com/iliazlobin/gcp-foundation); review and apply only the affected layer.

| Layer | Owns / operation guide |
| --- | --- |
| [Shared platform](https://github.com/iliazlobin/gcp-foundation) | Projects/APIs, VPC/NAT, private GKE, access VM and `shared-retain`; Gateway controller |
| [Shared application](environments/shared-development) | Workload identities, scoped IAM, registry, data/backup/state buckets and secret containers; [release/recovery](../../deploy/development.md) |
| [Consumer identity](environments/consumer-identity) | Identity Platform, restricted browser key, scoped identity IAM and empty RBAC parameter; [accounts](../../deployment/consumer-identity.md) / [operator setup](../../deploy/operator-access.md) |
| [Public access](environments/public-access) | Global IP, DNS authorizations, managed certificate/map, TLS policy and empty IAP secret container; [edge activation](../../deploy/public-access.md) |

Helm owns workloads, retained-store configuration, namespace policies and Gateway/routes;
the GKE controller owns load-balancer backends/NEGs. DNS authority owns the app A record
and certificate-validation CNAMEs. Terraform provisions secret/parameter containers, not provider
credentials, RBAC payloads or DNS records.
The shared application root also owns three protected CA recovery containers;
[TLS custody](../../deploy/development.md#authenticated-discovery-and-encrypted-dependencies)
keeps signing-key payloads outside state and workload grants.

## Shared application

[`environments/shared-development`](environments/shared-development) consumes the shared
platform's non-secret `platform_contract` for `iz27-platform-dev` / `platform-dev`. It owns the
application registry, workload identities and scoped resource access, secret containers, payload,
media and backup buckets, and its own protected state bucket. Foundation APIs/IAM, networking, node
identity, GKE, and `shared-retain` remain in the
[shared platform repository](https://github.com/iliazlobin/gcp-foundation).
The app Helm charts own namespace policies, releases, stores, and PVCs.

The `shared-development` root owns active application resources and separate state. Operation
helpers default to this destination and reject retired targets. The separate `development` root
remains until its retained resources have been removed and its state is empty; it cannot provision
a running application environment. Use the [deployment and recovery runbook](../../deploy/development.md);
`scripts/development/check_shared_plan.py` accepts only additions within the new app boundary.
The shared app's first reviewed plan bootstraps `iz27-platform-dev-ec-state` locally, after which
only that root's new state moves to the backend in `backend.tf.example`. Never migrate platform
or legacy state into it. Mocked validation is not evidence of a deployed stack.

## Alternate managed staging

The separate [staging root](environments/staging) defines private networking and NAT, a zonal GKE
control plane with nodes spread across three zones, regional Cloud SQL and Redis, a CMEK-backed GCS
claim-check bucket, Secret Manager **containers**, Artifact Registry, Workload Identity bindings,
and baseline monitoring. It deliberately does not create DNS, a public edge, Temporal Cloud, secret
versions, database passwords, or provider credentials. Use it only for a separately approved
managed-service deployment; it is not the shared in-cluster landing above.

Provider versions are exact and the state backend is GCS. Bootstrap the state bucket once with
uniform access, public-access prevention, versioning, and retention appropriate for infrastructure
state. That bucket should live in a separately administered project or have an independent destroy
boundary.

```bash
cd infra/terraform/environments/staging
cp staging.auto.tfvars.example staging.auto.tfvars
tofu init \
  -backend-config="bucket=YOUR_TERRAFORM_STATE_BUCKET" \
  -backend-config="prefix=events-concierge/staging"
tofu fmt -check -recursive ../..
tofu validate
tofu plan -out=staging.tfplan
```

Review the plan before an authorized operator applies it. Terraform creates no Secret Manager
versions. Populate each reported secret ID through an audited secret-rotation process, and pass the
reported Cloud SQL connection name, bucket, secret IDs, and service-account emails into the Helm
staging values.

Terraform reports the private Redis host, TLS port, and public server CA but never outputs the AUTH
token. In a secured operator session, retrieve the current token with `gcloud redis instances
get-auth-string ec-stg-redis --region us-west1 --project YOUR_PROJECT`, construct the `rediss://`
application URL so it requires the mounted CA file, and add that URL directly as a new version of
the reported `redis-url` secret. Populate the reported `redis-ca-certificate` container from the
public CA output. Rotate by adding a new secret version, rolling all consumers, proving sessions and
pacing on the replacement, and only then disabling the prior credential; never place either value
in a tfvars file or Terraform state input.

The Google provider marks the generated Redis AUTH string sensitive but necessarily records it in
Terraform state. Treat the encrypted, access-controlled remote state as credential material; this
stack never accepts the token as an input or exports it as an output. Durable data, secret
containers, keys, and the image repository also use `prevent_destroy`, so intentional teardown
requires a reviewed source change rather than only a command-line destroy.

The default Kubernetes endpoint is private. A GitHub-hosted runner cannot deploy to it directly;
use a self-hosted/network-connected runner or an approved private access path. If a public endpoint
is explicitly chosen for staging, set a narrow `master_authorized_networks` list—`0.0.0.0/0` is
rejected.

The first slice intentionally grants the runtime Python identities the same bucket, KMS, and
runtime-secret access because the current composition builds a complete dependency graph in each
process. The optional hosted operator profile uses the separate catalog composition: set
`operator_enabled = true` to provision its two identities and empty database-secret containers.
The controller can read only its own database secret. The executor can read its own database,
Redis/CA, and Temporal secrets and use objects only below `catalog_claim_check_prefix`; neither
identity receives consumer credentials, migration credentials, or envelope-key access. The
controller has no bucket access. GCS's service agent continues to handle bucket CMEK encryption.
The existing consumer identities retain their broad legacy bucket grants, which also cover
catalog objects; these changes constrain the new executor and do not establish mutual isolation
from the consumer runtime.

After authorized provisioning, `tofu output -json operator_helm_values` supplies the non-secret
`serviceAccounts.operator-api`, `serviceAccounts.ingestion-executor`, and
`operator.executorClaimCheckPrefix` chart overrides. Use the existing `secret_ids` output for
the new `operator-database-url` and `ingestion-executor-database-url` secret containers and pin
the audited versions in the chart. Database logins must inherit only `ec_operator_controller`
or `ec_ingestion_executor`, respectively; provisioning secret payloads, login passwords, IAP,
TLS, and enabling `operator.enabled` remain separate release steps. The catalog prefix must
remain disjoint from the consumer claim-check prefix. Existing catalog workflows must be drained
or proven compatible before changing their storage prefix.

`tofu test` in the staging directory checks both profiles using mocked providers and plan-only
runs. The storage module has a separate mocked plan test for its catalog prefix IAM boundary.

The two included alert policies are baseline infrastructure signals, not launch-complete
observability. Before production traffic, add API availability/error/latency, failed Kubernetes
Job, Temporal poller/backlog/schedule-to-start, durable-outbox age, and stuck-worker alerts backed by
tested notification routes. Staging requires a notification email so even the baseline policies
cannot be applied silently.
