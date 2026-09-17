# Private GKE development

This runbook operates the shared `platform-dev` development deployment and preserves the legacy
`ec-dev` recovery procedure. Shared foundation, networking and GKE are owned by [gcp-foundation](https://github.com/iliazlobin/gcp-foundation);
do not create a second cluster from this application root for that destination.
Product scope and release gates: [first-release acceptance](../docs/production-operations.md#first-release-acceptance).

The shared discovery deployment has passed restoration, user-flow, crawler and recovery acceptance.
Legacy application releases, the `ec-dev` cluster/node pool and its dedicated network/NAT are retired.
Legacy foundation, state, backups, identities and two retained PostgreSQL disks remain with their
existing owners for recovery. Access remains IAP and loopback-only; production gates remain open.
Historical commands must not be silently repointed to the shared project.

On a shared destination, the platform must install and verify its retained `shared-retain`
StorageClass before application stores are installed. Set `createStorageClass=false` and
`storageClass=shared-retain` on the data chart; application Helm must not own the platform class.
The retired cluster’s `ec-dev-retain` class and claims no longer provide a live endpoint; its
two retained cloud disks remain with the legacy application owner.

This profile uses real PostgreSQL, Redis, Temporal and GCS with explicit mock external product adapters. No real email, booking or Calendar actions. The older staging Terraform root remains separate.

## Current deployment

The shared cluster `platform-dev` in `iz27-platform-dev/us-west1-a` runs private discovery in
`events-concierge-dev`. Read-only inspection on September 17 found all six application Deployments,
both PostgreSQL StatefulSets, Redis and the four Temporal server Deployments ready. Services are
ClusterIP-only with no Ingress. The ingestion cadence CronJob is enabled and schedules every five
minutes; it checks due sources rather than refreshing every source on every tick.

The API health/readiness checks pass with schema `0194` and identity `not_configured`. Its
reported revision is `d5cac84111eeea2fdf8329dcedc5d937e2a20390`. The running images are:

| Component | Artifact Registry image digest |
| --- | --- |
| Backend | `sha256:d6b523158cb08d910e24b2242ebfe366a972f0aaf52246b36e2e36b10bb66899` |
| Frontend | `sha256:c80be4f2664c80f08e1dfae0607131be250a36a2489816fa394cd222c7a741bf` |

The running profile is still `development` / `discovery`: `EC_MOCK_CLOUD=true`,
`EC_OIDC_BFF_ENABLED=false`, `EC_DATABASE_CONNECTION_MODE=development_plaintext` and
`EC_TEMPORAL_TLS_ENABLED=false`. PostgreSQL, Redis, Temporal, catalog collection and GCS are real;
consumer identity and deferred product integrations use the development behavior. The deployed
images predate the current `main`. Healthy pods do not establish acceptance of the new candidate.

Google sign-in, startup safeguards, datastore TLS and self-hosted Temporal mTLS preparation are
merged into `main`. The source migration head is `0195`; the live database remains at `0194`.
Merging or passing CI does not publish images, migrate data or update the cluster. Releases remain
manual and require one reviewed commit with immutable backend/frontend image digests.

### Remaining release work

1. Implement an authenticated private application Helm profile for the existing in-cluster stores.
   The development profile requires mock integrations/plaintext PostgreSQL and disables OIDC;
   the managed profile requires Cloud SQL Proxy. Neither is the desired private authenticated
   composition. Add per-process Secret/IAM and certificate mounts rather than bypassing those guards.
   The account-erasure worker currently uses full application/BFF preflight; preserve its session
   revocation and cleanup responsibilities when defining its minimum credentials.
2. Obtain the remaining identity approvals and complete configuration: a separate Events Concierge Google OAuth client,
   exact `https://localhost:14443/auth/callback`, verified subject-to-tenant provisioning and trusted
   private browser HTTPS. A private Google pilot requires an explicit owner decision to keep
   self-service account deletion unavailable until independent reauthentication exists. Do not
   reuse Symphony's OAuth client. [Identity contract](../docs/production-operations.md#built-in-oidc-bff-activation).
3. Rehearse the combined candidate, migration and retained-data transport rollback. Confirm node
   capacity for steady workloads, cadence, migration hooks and rollout overlap. Deliver versioned
   certificates and restricted credentials through the approved secret mechanism.
4. Obtain deployment authorization for the concrete candidate and configuration. Suspend cadence,
   drain writers, take and verify a fresh shared backup, then coordinate datastore/client transport,
   migration and application rollout. Preserve PVCs and restored Temporal databases.
   [Transport cutover](#encrypted-dependency-preparation) and [recovery](#manual-recovery) own the commands.
5. Verify actual TLS/mTLS handshakes and rejection cases, serving revision/digests, Google
   login/logout, CSRF, tenant isolation, discovery/crawler behavior and worker recovery on GKE.
   Close application monitoring, alert routing, scheduled backups and restore checks before
   claiming production acceptance. Independent project-loss recovery remains deferred.

The existing [verified shared recovery set](https://console.cloud.google.com/storage/browser/_details/iz27-platform-dev-ec-backups/20260912T014120Z-08707790/VERIFIED.json?project=iz27-platform-dev&authuser=4)
and [deployed discovery acceptance evidence](https://console.cloud.google.com/storage/browser/_details/iz27-platform-dev-ec-backups/20260912T014120Z-08707790/acceptance.json?project=iz27-platform-dev&authuser=4)
cover the earlier development release, not the pending authenticated candidate. The September 17
metadata check found no newer completed/verified shared backup set and no Cloud Monitoring alert
policies in `iz27-platform-dev`. Backups remain manual. Redis persistence survived its retained-disk recovery drill but is outside the database/
payload backup; avatar media is intentionally excluded so erasure can delete it.
The [legacy retirement readback](https://console.cloud.google.com/storage/browser/_details/iz27-platform-dev-ec-backups/20260912T014120Z-08707790/retirement.json?project=iz27-platform-dev&authuser=4)
records the old cluster/network removal. Retained legacy foundation, state, identities, backups
and PostgreSQL disks remain recovery dependencies; routine release work must preserve them.

## Shared application landing

The platform owns the two projects, deployment identities, network, GKE/node pool, private access
VM and `shared-retain`. Follow its [private access and storage gates](https://github.com/iliazlobin/gcp-foundation#private-access)
before installing application resources. The app-owned [shared-development root](../infra/terraform/environments/shared-development)
owns its registry, workload identities, secrets and payload, media, backup and state buckets. It never creates a
cluster or changes shared network resources. The legacy root/state retain ownership of their recovery resources.

Use the platform's verified `platform_contract` output as the new root's input. Review its exact
saved plan with `scripts/development/check_shared_plan.py`. Bootstrap the app root into fresh local
state, then migrate only that state to its protected `iz27-platform-dev-ec-state` bucket as described
in its [backend example](../infra/terraform/environments/shared-development/backend.tf.example).
Keep plan, credentials and state out of Git. Do not initialize the legacy backend for this destination.

All commands use the platform's separate kubeconfig and loopback IAP tunnel. The context must be
`gke_iz27-platform-dev_us-west1-a_platform-dev`; the namespace remains `events-concierge-dev`.
The helpers' default target is **legacy**. Select `--target shared` explicitly for secret bootstrap,
release-value generation, readiness, store-recovery drills and backups. Use `.venv/bin/python`.

Layer `values-shared-development.yaml` after `values-development.yaml` for the application, and
the data chart's shared overlay after its defaults. These select the shared project/bucket,
discovery-only UI/API and platform-owned storage. The shared overlay omits transactional,
request-start, notification and change-delivery workers so restored deferred work cannot resume. Catalog and
account-erasure workers remain active; shared readiness requires all six intended deployments and
rejects any deferred Deployment. Image digests belong to the selected committed
candidate in `us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/`. They must pass combined CI before
deployment. This remains a private development candidate until real identity and the separate
[production gates](../docs/production-operations.md#first-release-acceptance) pass.

Manual application CI retains a three-day `candidate-images-COMMIT` artifact in this private
repository after its image startup/canary checks. Download only from the successful run for the
selected commit, verify `SOURCE_REVISION` and `SHA256SUMS`, then load the archive. Verify both image
revision labels, tag and push those same images to the private Artifact Registry, and record their
registry digests in release values. Accept the package only after every CI and deployment check
for that commit passes. The archive is a temporary release artifact, not a backup or deployment.

Restore the coordinated legacy databases and payloads into the new stores before starting writers;
preserve tenant/request data, schema compatibility and provenance. Take a fresh verified backup
with old writers stopped for final cutover. Do not substitute local test fixtures for production data.
Verify restoration, user workflows, private-only Services and worker execution before accepting it.

For the schema-0193 backup, install only stores and require an empty application schema and empty
Temporal databases. Before `pg_restore --exit-on-error`, create restricted `ec_app` with the **new**
pinned app-role password; migration 0002 will not rerun after restoration. Create NOLOGIN
`ec_operator_viewer`, `ec_operator_controller`, `ec_ingestion_executor` and NOINHERIT
`ec_operator_aggregate_definer`, granting viewer to controller. Preserve original owners/ACLs:
`ec_owner` and the aggregate definer for the app, `temporal` for its databases. Keep the two
`ec_dev_*` logins absent so the existing migration bootstrap creates them from new pinned secrets.
Do not run Temporal schema-init jobs before restoring. Copy the exact payload hierarchy; the
content keys and Temporal history need no bucket-URI rewrite. Then run candidate migration/bootstrap
and verify manifest counts, isolation and historical payload reads before starting writers.

The old backup excludes Redis. At final quiesce, inventory provider backoffs, admission fences and
sessions again; preserve or wait out outstanding backoffs before a cold start. Do not infer an empty
store from the earlier one-key pacer snapshot. New backups inventory and hash every payload-bucket
object. Older payload-only backups retain their narrower verification.

### Shared release and access

Keep the platform's IAP tunnel running and export the separate kubeconfig produced by its
[private-access procedure](https://github.com/iliazlobin/gcp-foundation#private-access). Verify the
shared context named above before each release. These commands assume the reviewed app Terraform
apply and state migration are complete, and `.local/shared-release-values.yaml` was generated with
`release_values.py --target shared` from the passing candidate's registry digests.

For new stores only:

```bash
kubectl create namespace events-concierge-dev --dry-run=client -o yaml | kubectl apply -f -
.venv/bin/python scripts/development/bootstrap_secrets.py --target shared --project-to-kubernetes
helm upgrade --install ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml --wait --timeout 10m
```

Restore the verified snapshot and restricted roles as described above, and require TCP readiness
before each restore. After verifying the restored databases and payloads, run the retained-volume
replacement drill while application and Temporal writers are still absent:

```bash
.venv/bin/python scripts/development/check_store_recovery.py --target shared
helm upgrade --install events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f deploy/helm/events-concierge/values-shared-development.yaml -f .local/shared-release-values.yaml --wait --wait-for-jobs --timeout 10m
helm upgrade --install ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml --set server.config.persistence.datastores.default.sql.manageSchema=false --set server.config.persistence.datastores.visibility.sql.manageSchema=false --set server.config.persistence.datastores.visibility.sql.createDatabase=false --set server.config.namespaces.create=false --wait --timeout 15m
helm upgrade events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f deploy/helm/events-concierge/values-shared-development.yaml -f .local/shared-release-values.yaml --set global.releasePhase=application --set global.runtimeProviderReady=true --wait --timeout 10m
.venv/bin/python scripts/development/wait_ready.py --target shared
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/promote_workers.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/smoke.py
```

The restored Temporal options disable database/schema/namespace initialization; the chart retains
only a harmless completion hook. They apply to this restored environment, not a fresh empty Temporal
installation. The application migration runs before its writers and must preserve the restored data.

For subsequent releases, preserve the accepted cadence and replica settings after the backup and
migration gates. Render from the three source value files; do not use `helm get values --all` as
an input file, because resolved null-removal semantics can reintroduce omitted defaults.

```bash
helm upgrade events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f deploy/helm/events-concierge/values-shared-development.yaml -f .local/shared-release-values.yaml --set global.releasePhase=application --set global.runtimeProviderReady=true --set developmentCatalog.cadenceEnabled=true --set workloads.api.replicas=1 --wait --timeout 10m
```

Run each port-forward in its own terminal, with the same dedicated shared kubeconfig:

```bash
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-api 14000:8000
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-frontend 14001:80
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 deployment/events-concierge-admin 14002:3000
```

Use `http://127.0.0.1:14001` for consumer acceptance and `http://127.0.0.1:14002/admin` for private
administration. These forwards grant no public access. Shared backup/verify/resume commands must
include `--target shared`; never use a legacy recovery prefix against the new cluster.

If the IAP transport exits, restart it with the platform private-access helper, verify the shared
context and node again, then restart these three forwards. Restart affected forwards after a pod
rollout too: even a Service forward stays attached to its initially selected pod. A running
`kubectl port-forward` process does not prove that its IAP connection or selected pod remains available.

The private HTTPS proxy prepares `https://localhost:14443` for the planned Google sign-in release,
with the intended exact callback `https://localhost:14443/auth/callback`. The application startup checks
support that exact browser origin with `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`;
it still requires real identity, encrypted dependency connections and restricted runtime credentials
before a non-mock process starts. Google provider code is integrated, while its client secret,
verified subject-to-account mapping and encrypted dependency rollout remain pending.
The current shared development overlay still uses local-demo identity.
[Identity activation](../docs/production-operations.md#built-in-oidc-bff-activation) owns the existing
OIDC contract, Google configuration and remaining production prerequisites. These proxy instructions
alone do not authorize activation.

Keep the frontend forward above running. Supply a certificate valid for `localhost`, trusted by
the operator's browser, and its protected private key using `EC_LOCAL_TLS_CERT` and
`EC_LOCAL_TLS_KEY` (absolute paths outside Git), then run:

```bash
caddy validate --config deploy/private-access.Caddyfile --adapter caddyfile
caddy run --config deploy/private-access.Caddyfile --adapter caddyfile
```

The [private proxy configuration](private-access.Caddyfile) binds only `127.0.0.1:14443`, preserves
the browser Host, and proxies the loopback frontend forward. Its admin listener, automatic HTTP
redirects and automatic trust installation are disabled. Obtain trust through the operator's
approved certificate setup; never bypass a browser certificate warning. No public DNS, ingress,
load balancer or firewall opening is needed. Local TLS does not encrypt the app's database,
Redis or Temporal connections; those and live Google login/CSRF/logout acceptance remain separate
release gates. Caddy 2.11.4 passed configuration and real TLS proxy checks with an isolated test
certificate, including hostname-mismatch rejection; browser trust and deployed Google login are
not established by that check.

### Encrypted dependency preparation

The opt-in [data TLS values](helm/events-concierge-dev-data/values-private-tls.yaml) and
[Temporal TLS values](helm/temporal-private-tls.yaml) prepare an encrypted private release. The
active development overlays remain plaintext/local-demo. These files do not provision certificates,
change the application secret mounts or enable non-mock startup. Application Helm and its secret
ownership plan still need an explicit authenticated profile; do not bypass their development guards.

| Connection | Required contract |
| --- | --- |
| Application and operator PostgreSQL | Service FQDN, `sslmode=verify-full`, `PGSSLROOTCERT` pointing to a CA-only mount; preserve each process's restricted DB role |
| Temporal PostgreSQL | Service FQDN in both SQL `connectAddr` values, verified TLS and CA-only mount; restored database/schema/namespace initialization stays disabled |
| Application Redis | `rediss`, trusted CA file, required certificate verification and hostname checking; existing password authentication |
| Application to Temporal | Frontend Service FQDN, server CA and separate client certificate/key files with `clientAuth` usage |
| Temporal internode and internal frontend clients | Server certificate with `serverAuth` and `clientAuth`, trusted client CA, verified server names |

Each datastore Secret named in the values file contains `ca.crt`, `tls.crt` and `tls.key`. Issue
server certificates with the exact Service FQDN in the DNS subject alternative names (SANs); use a
short common name because a full Service FQDN can exceed its length limit. Temporal's server
certificate additionally needs the SAN `ec-dev-temporal-internode`.
`ec-dev-postgres-ca-v1` contains **only** `ca.crt`; PostgreSQL private keys never go to Temporal or
application containers. Keep signing keys outside workloads and Git. Use separate, versioned client
credentials per allowed workload and mount no Google/OIDC secret into operator or catalog processes.

The PostgreSQL/Redis charts stage private keys in memory with image-native ownership and `0600`
permissions. Versioned Secret names are part of the pod template: rotate by creating the next
Secret version and rolling the workload. Updating a Secret's contents alone does not refresh that
staged copy. Redis's loopback probe verifies CA and password; Redis 7 `--sni` does not verify the
hostname. Application clients must additionally enforce hostname verification.

Inspect the candidate without changing a cluster:

```bash
helm template ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml -f deploy/helm/events-concierge-dev-data/values-private-tls.yaml
helm template ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml -f deploy/helm/temporal-private-tls.yaml
EC_HELM_BINARY=helm EC_DATA_TLS_DOCKER=1 .venv/bin/python -m pytest tests/unit/test_development_data_tls.py -q
```

Activation is a coordinated maintenance operation, not a rolling flag change:

1. Review application-owned Secret/IAM/volume wiring, certificate lifetimes and recovery access.
   Verify **every** application/operator/executor and Temporal login already has a SCRAM verifier;
   record booleans, never password hashes. The new PostgreSQL HBA rejects plaintext and requires
   SCRAM for every TCP login. A successful owner readiness probe does not validate other roles.
2. Verify shared-node CPU/memory request headroom for existing pods, cadence, migration hooks and
   rollout overlap. A 90-second cadence deadline includes scheduling time. `FailedScheduling` or
   `NotTriggerScaleUp` must be resolved before rollout; do not add Symphony-worker tolerations.
3. Rehearse retained-data TLS conversion and rollback in isolation. Capture a fresh, verified backup,
   quiesce cadence/application/Temporal writers and inventory Redis session/pacing state. Preserve
   PVC identities and existing database contents throughout; never initialize restored Temporal
   databases. The chart retains an echo-only schema completion hook with schema mutations disabled.
4. Change datastore listeners and all corresponding clients together; keep writers stopped until
   verified SQL queries, Redis commands and an actual Temporal mTLS handshake succeed. Verify
   plaintext, wrong CA/hostname and untrusted/missing client certificate failures. Resume intended
   writers only after migration and authenticated application acceptance pass.
5. Roll back by stopping writers and restoring the prior reviewed listener/client configuration as
   one unit. Retain certificate versions and PVCs; do not restore an older database over new writes
   without a separate data-recovery decision. Recheck readiness and queue continuity before resume.

Local pinned-image tests establish the listener and client contracts. They do not establish GKE
certificate delivery, Temporal authorization, browser identity or a completed deployment. The
[Temporal operations contract](../docs/production-operations.md#temporal) describes client settings
and the self-hosted server's authorization limit.

Avatars use the separate private `iz27-platform-dev-ec-media` bucket, with no versioning, soft
delete or retention so account erasure can remove them. Only the API, private admin and erasure
worker can access it; the adapter verifies this policy before accepting destructive completion.
Media is excluded from retained payload backups. Database recovery may require users to reupload
avatars; never claim a database/payload restore recovered media. Verify upload, replica-independent
read, deletion and account erasure on the shared deployment.

The completed September 11 initial cutover is recorded below; do not replay these configuration
changes as routine release steps. The registry comparison found the same 105 non-fixture registrations in both stores;
no source inserts or table import are needed. After restoration and migration, use optimistic,
audited admin configuration changes to match the local reviewed set: enable `berkeley-events`,
`berkeley-public-library-events`, `berkeley-rep-shows`, `scu-events`, `sf-gov-related-events`,
`sjsu-events`, `smccd-events`, `stanford-events` and `ucsf-events`; disable `luma-nyc-nyaiengineers`.
Set the resulting 92 enabled sources to a 1,440-minute refresh interval. First recheck source keys
and immutable settings; abort on unexpected drift. Preserve destination revisions, review/history
metadata, URLs, collection windows and retirement records. The local database also contains 497
fixture registrations: exclude them using `fn_ingestion_admin_source_is_fixture`, never copy them.

For collection acceptance, compare `fn_report_catalog_source_coverage_v1()` on both environments,
using the same as-of time and collection windows. It excludes fixtures. Require every reviewed source
to have current successful execution or an explicitly investigated failure, and compare per-source
live future events and freshness; summed links can count one event from multiple sources. Imported
counts alone do not prove crawling. Keep the old stack until this parity and recovery gate passes.

After a real queued refresh succeeds through the separate ingestion executor, opt in to
`developmentCatalog.cadenceEnabled=true`. This creates a five-minute CronJob that queues reviewed
due sources through the controller credential; it does not fetch providers or enable scheduling in
the consumer API. Verify the CronJob, command ledger and resulting successful refresh runs separately.
The legacy dispatcher stays disabled. A successful schedule tick alone does not prove collection.
Scheduled `refresh_due` runs currently lack the flattened run-level release fields in the admin
projection. Verify the linked command's executor revision/digest and actual worker deployment;
this projection gap remains an observability follow-up, not missing command execution evidence.
`promote_workers.py` selects only catalog in discovery and verifies current candidate pollers before
promotion. `smoke.py` selects catalog/profile/deferred-route checks in discovery, with a separate
Temporal/GCS echo; it reports synthetic cleanup as pending until the erasure worker completes it.

Before any backup, suspend the cadence CronJob and wait for all unfinished Jobs to terminate.
The backup helper refuses running schedules or unfinished Jobs before stopping writers and checks
again before dumping. Keep schedules suspended during cutover/recovery. Restore their previous
suspension state only after writer readiness and backup/restore verification; `backup resume`
restores saved Deployment replicas, not CronJob schedules.

Retire old application releases, then their old GKE/node resources and dedicated network/NAT only
after accepted destination parity, user flows and recovery. Review each owning root's exact removal
plan; preserve backups, state and retained disks while their recovery or dependency purpose remains.

## Legacy infrastructure

<details>
<summary>Historical ec-dev deployment and recovery procedure</summary>

These commands describe the old app-owned cluster. The current legacy Terraform root preserves
recovery resources and removes its cluster/node pool; it cannot provision the historical stack.
Use the [pre-retirement source](https://github.com/iliazlobin/events-concierge/tree/d5cac84111eeea2fdf8329dcedc5d937e2a20390)
only for a separately reviewed recovery. Do not execute this procedure against the shared context.


Use Terraform 1.16.1, gcloud, kubectl with gke-gcloud-auth-plugin, Helm 3, Python 3.12+ with PyYAML, and Docker Buildx. Authenticate as `iliazlobin27@gmail.com`; target `project-9c8cce04-f94d-40fc-aa6` explicitly. Never copy local credentials to another machine.

Initialize the repository environment with `uv sync --frozen --python 3.12`. Run the backup helper with `.venv/bin/python`; the macOS system `python3` can be too old.

From the repository root:

```bash
mkdir -p .local
cp infra/terraform/environments/development/backend.tf.example infra/terraform/environments/development/backend.tf
export TF_VAR_impersonate_service_account=iac-development-rw@iz27-foundation.iam.gserviceaccount.com
terraform -chdir=infra/terraform/environments/development init
terraform -chdir=infra/terraform/environments/development plan -out=../../../../.local/development.tfplan
terraform -chdir=infra/terraform/environments/development show -json ../../../../.local/development.tfplan > .local/development-plan.json
python3 scripts/development/check_plan.py .local/development-plan.json
# Review the exact plan before applying. The initial scope gate permits additions only.
terraform -chdir=infra/terraform/environments/development apply ../../../../.local/development.tfplan
```

State is in `gs://iz27-foundation-development-state/events-concierge/development`. Foundation owns networking/NAT and deployer grants; this root only reads its existing network. The fixed node is e2-standard-2; changing size requires a reviewed capacity/cost decision. Deletion protection and retained database PVCs intentionally block accidental removal. The deployer has powerful development-project IAM; runtime identities have separate scoped grants.

## Connect and initialize a new environment

```bash
export KUBECONFIG="$PWD/.local/kubeconfig"
gcloud container clusters get-credentials ec-dev --zone=us-west1-a --dns-endpoint --project=project-9c8cce04-f94d-40fc-aa6 --account=iliazlobin27@gmail.com
kubectl create namespace events-concierge-dev --dry-run=client -o yaml | kubectl apply -f -
.venv/bin/python scripts/development/bootstrap_secrets.py --project-to-kubernetes
helm upgrade --install ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev --wait --timeout 10m
helm upgrade --install ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml --wait --timeout 15m
```

For an existing environment, connect using its task-local kubeconfig and follow the application cutover below. Do not rerun the data-chart installation as part of an application update.

Secret values are generated directly into Secret Manager. Version 1 is pinned and existing credentials are reused; rotation requires a reviewed release. Databases and Redis use separately projected Kubernetes secrets; application pods use the GKE Secret Manager CSI driver.

## Application release

The development admin uses a separate `ec_dev_operator` login with controller capability. The command executor and catalog Temporal worker share an `ec_dev_ingestion` login with executor capability; each has its own workload identity. Both catalog processes use GCS under `events-concierge/catalog/v1`. The consumer `ec_app` role has neither capability.

### Prepare and review

1. Select one committed candidate with passing CI. Build the backend and `web/Dockerfile` for `linux/amd64`, using that full commit as backend `VCS_REF`. Publish to `us-west1-docker.pkg.dev/project-9c8cce04-f94d-40fc-aa6/ec-dev/` and retain the immutable image digests.
2. Save and review the Terraform plan. For the operator cutover, run `python3 scripts/development/check_plan.py --operator-cutover .local/development-plan.json`. This permits only the two exact old catalog IAM revocations in addition to normal additions; default mode remains initial-additions only. Apply after quiescing below, because the old catalog worker still needs those grants.
3. Rehearse the candidate migration and logins against disposable PostgreSQL. Existing login collisions and pinned-password mismatches must fail before Alembic. Check controller/executor separation, catalog payload exchange, and the rendered private-development profile. Keep the customer release-scope decision separate from this private development update.

### Quiesce and back up

Disable access/new submissions and wait for current work to settle. Confirm there are no open product workflows in Temporal, active catalog/command leases or pending request-start/notification records. Internal worker-version tracking workflows are expected and are not product work. Repeat the check after application workers stop to close the race. Worker-version promotion does not move existing pinned workflows to the new version; do not remove workers needed by outstanding executions.

```bash
.venv/bin/python scripts/development/backup.py backup --hold-stopped
.venv/bin/python scripts/development/backup.py verify gs://iz27-ec-dev-backups/SET_ID
```

Use the exact completed set printed by backup. Before stopping anything, the helper saves and reads back password-free `recovery.json` in that same prefix with the schema version, original writer replica counts and deployment identities. It then saves all three PostgreSQL databases, payloads and image/schema metadata. Application writers stop before Temporal; PostgreSQL and Redis remain running. A failed backup attempts to restore writer replicas with bounded retries. A successful `--hold-stopped` backup leaves writers stopped until the cutover is accepted. Do not run the data-chart upgrade here: Redis persistence and PostgreSQL probe changes require their own storage rehearsal.

If backup or automatic recovery is interrupted, restore connectivity and run the printed recovery command **before any migration or rollout**:

```bash
.venv/bin/python scripts/development/backup.py resume gs://iz27-ec-dev-backups/SET_ID
```

Resume can be repeated. It restores only saved replica counts, starts Temporal first, and refuses unknown names, counts outside 0/1, replaced Deployments, changed workload templates or a changed schema. Check every original Deployment is ready before reopening access. An incomplete prefix may contain recovery metadata and partial dumps; preserve it as failure evidence, but never use it for data restoration. Older backups without `recovery.json` require their saved manifest and reviewed manual recovery. After migration begins, follow the compatible-image recovery procedure below; `resume` is not a schema rollback.

### Apply credentials and application

Apply the reviewed Terraform plan while the old workers are stopped, then initialize the new pinned secret versions. Existing versions and login passwords are preserved.

```bash
terraform -chdir=infra/terraform/environments/development apply ../../../../.local/development.tfplan
python3 scripts/development/bootstrap_secrets.py
.venv/bin/python scripts/development/release_values.py --app-image "$APP_IMAGE" --web-image "$WEB_IMAGE" --revision "$BACKEND_REVISION" --output .local/release-values.yaml
helm upgrade --install events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f .local/release-values.yaml --wait --wait-for-jobs --timeout 10m
```

The migration Job preflights existing reserved logins and authenticates their pinned credentials, upgrades the schema, provisions missing logins, then authenticates both. It never grants operator authority to `ec_app`. If bootstrap fails after Alembic commits, keep the application stopped and repair/retry the Job. Migration `0187` has no downgrade: **do not restart schema-0180 images or use Helm rollback after schema-0193 migration**. Recovery uses the verified coordinated database/payload backup and compatible images.

The migration Helm phase removes the old application Deployments. Restore the four `ec-dev-temporal-*` server Deployments to the replica counts in the backup manifest and wait for readiness, then recreate the application:

```bash
helm upgrade events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f .local/release-values.yaml --set global.releasePhase=application --set global.runtimeProviderReady=true --wait --timeout 10m
python3 scripts/development/wait_ready.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/promote_workers.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/smoke.py
```

### Acceptance and access

- Require all ten application/admin/executor Deployments to have one updated, available, ready replica. Helm readiness alone can accept zero ready replicas with `maxUnavailable=1`.
- Verify schema, separate controller/executor login access, API/admin reads, Temporal pollers and catalog-prefix GCS access. Process probes alone do not prove worker execution.
- `smoke.py` proves a completed synthetic request workflow and a GCS round trip; a business outcome such as `failed_no_candidate` is not a successful registration. Isolated catalog/Temporal tests cover executor and publication behavior. A deployed catalog end-to-end check needs a reviewed source refresh; do not bypass fixture exclusion or insert fixture sources in the runtime database.
- Create and verify a fresh schema-0193 backup before accepting the cutover. Preserve the pre-cutover backup.

```bash
kubectl -n events-concierge-dev port-forward service/events-concierge-frontend 13000:80
```

Browse http://localhost:13000. Access remains localhost-only. Recurring jobs and autoscaling remain disabled. One node and one replica mean downtime during replacement and upgrades; disks remain zonal. This is private development, not production.

</details>

## Persistent storage and self-healing

The shared deployment uses retained PostgreSQL and Redis disks. Before starting its writers on
September 11, replacement of each store pod preserved application/Temporal database markers and
the Redis marker on the same PVC/PV bindings. This proves pod replacement recovery, not disk-loss
or zone-loss recovery. Legacy Redis was ephemeral and was not included in the coordinated backup.

Application PostgreSQL and Temporal PostgreSQL each mount a retained 20 GiB GCP `pd-balanced` disk. Shared Redis mounts a retained 10 GiB disk at `/data`, with AOF synced every second and periodic RDB snapshots. Redis can lose roughly the last second of writes in a crash; persistence does not make it highly available. Its single-replica Deployment uses `Recreate` so updates stop the old writer before starting the replacement.

Startup probes allow database recovery before liveness checks begin. Failed processes restart; controllers replace missing pods and remount their PVCs. GKE node auto-repair and auto-upgrade are enabled. Disks remain in `us-west1-a`: a node replacement can reattach them, but node repair causes downtime and a zone outage needs separate recovery. Retained disks are not backups; the manual GCS database/payload backup remains the recovery path for those stores. Redis disk loss requires separate restoration/reconstruction; Redis is not included in that GCS backup routine.

## Private admin

The development admin runs in `events-concierge-admin`: its frontend and API both bind to pod loopback. There is no Service; access requires Kubernetes port-forward permission. The ordinary API keeps administration disabled. Shared ingestion uses the separately enabled cadence CronJob.

```bash
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 deployment/events-concierge-admin 14002:3000
```

With the shared kubeconfig, open http://127.0.0.1:14002/admin. The dedicated pod uses the shared live development database and current immutable images. Its readiness checks exercise the admin overview through both API and frontend. Legacy recovery requires a separately reviewed recovery environment; the archived procedure does not provide a running legacy endpoint.

## Manual recovery

Use the dedicated shared kubeconfig. Record the cadence CronJob's current suspension state, suspend
it, and wait for unfinished Jobs and active ingestion commands to finish before backup. Check the
private admin command/run views; a completed scheduler Job can leave its collection command running.

```bash
kubectl -n events-concierge-dev patch cronjob events-concierge-ingestion-cadence --type=merge -p '{"spec":{"suspend":true}}'
# After all scheduled Jobs and collection commands finish:
.venv/bin/python scripts/development/backup.py backup --target shared
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
.venv/bin/python scripts/development/wait_ready.py --target shared
```

Restore the CronJob's previous suspension state only after verification and writer readiness.
Restart affected loopback forwards after the writer pods are replaced. If backup recovery is
interrupted, use the exact prefix printed before quiescing:

```bash
.venv/bin/python scripts/development/backup.py resume gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

Resume restores saved replicas; it neither restores data nor changes images. It refuses changed
schema, deployment identity or pod templates. Investigate those mismatches instead of forcing resume.

Backup temporarily stops application writers and Temporal while retaining PostgreSQL/Redis, exports all three databases and current payload objects, records image/schema metadata, uploads a completion marker last, then attempts to restore writer replica counts. Only completed sets are restoration candidates. Keep at least three successful sets; no automatic deletion is configured. Verification restores dumps into disposable local Docker databases and validates checksums; the live smoke test also checks workflow completion and real GCS claim checks. Use `smoke.py --repeat 12` inside the API pod for a small sustained development load test. Production disaster recovery remains separate. Never restore over the running development stores. Backups share the same project administrative boundary.
