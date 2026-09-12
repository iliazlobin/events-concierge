# Private GKE development

This runbook operates the shared `platform-dev` development deployment and preserves the legacy
`ec-dev` recovery procedure. Shared foundation, networking and GKE are owned by [gcp-foundation](https://github.com/iliazlobin/gcp-foundation);
do not create a second cluster from this application root for that destination.
Product scope and release gates: [first-release acceptance](../docs/production-operations.md#first-release-acceptance).

The shared platform and app resources are applied; destination acceptance is in progress. The legacy
writers are stopped with a verified final backup. Preserve its stores and infrastructure until restored
data, user workflows, crawler parity and recovery pass. Commands in the legacy sections still target
the existing project and must not be silently repointed to the shared project.

On a shared destination, the platform must install and verify its retained `shared-retain`
StorageClass before application stores are installed. Set `createStorageClass=false` and
`storageClass=shared-retain` on the data chart; application Helm must not own the platform class.
Existing `ec-dev-retain` and its bound legacy volumes keep their current ownership.

This profile uses real PostgreSQL, Redis, Temporal and GCS with explicit mock external product adapters. No real email, booking or Calendar actions. The older staging Terraform root remains separate.

## Current verification

On September 11, the shared cluster passed the actual Next.js/API discovery walkthrough, private
admin reads, keyboard/mobile navigation, loading/error recovery, profile and saved-filter changes,
logout, two-tenant isolation and completed worker erasure. Additional live checks covered multiple
sources/cities/scopes, additive date ranges, all price comparisons, availability/sorting and
Week/Month/six-month Calendar navigation. Avatar bytes matched across the browser,
two API replicas and private GCS; explicit deletion and account erasure left no tenant media.
The backend runs `d5cac841`; frontend `f3fc7c9` fixes cleared-city URL reloads. Backend runtime code
is unchanged between those revisions; all six application deployments now have one ready replica.

All eight checks passed `f3fc7c9`: 2,238 unit tests plus 11 subtests, 409 integration tests,
40 quality scenarios, 253 browser tests, 584 frontend tests, 10 restore checks, 37 container canary
checks and deployment validation. [Application CI](https://github.com/iliazlobin/events-concierge/actions/runs/34662322573)
· [Deployment checks](https://github.com/iliazlobin/events-concierge/actions/runs/34662323874).
These private development checks do not close real identity/CSRF, transport, monitoring,
per-process permissions or non-mock provider-cleanup gates.

## Shared application landing

The platform owns the two projects, deployment identities, network, GKE/node pool, private access
VM and `shared-retain`. Follow its [private access and storage gates](https://github.com/iliazlobin/gcp-foundation#private-access)
before installing application resources. The app-owned [shared-development root](../infra/terraform/environments/shared-development)
owns its registry, workload identities, secrets and payload, media, backup and state buckets. It never creates a
cluster or changes shared network resources. The old root/state retain their ownership until retirement.

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
discovery-only UI/API and platform-owned storage. The shared overlay omits transactional, request
starter, notifier and change-delivery workers so restored deferred work cannot resume. Catalog and
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

Avatars use the separate private `iz27-platform-dev-ec-media` bucket, with no versioning, soft
delete or retention so account erasure can remove them. Only the API, private admin and erasure
worker can access it; the adapter verifies this policy before accepting destructive completion.
Media is excluded from retained payload backups. Database recovery may require users to reupload
avatars; never claim a database/payload restore recovered media. Verify upload, replica-independent
read, deletion and account erasure on the shared deployment.

The September 11 registry comparison found the same 105 non-fixture registrations in both stores;
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
or zone-loss recovery. The stopped legacy Redis deployment remains ephemeral.

Application PostgreSQL and Temporal PostgreSQL each mount a retained 20 GiB GCP `pd-balanced` disk. Shared Redis mounts a retained 10 GiB disk at `/data`, with AOF synced every second and periodic RDB snapshots. Redis can lose roughly the last second of writes in a crash; persistence does not make it highly available. Its single-replica Deployment uses `Recreate` so updates stop the old writer before starting the replacement.

Startup probes allow database recovery before liveness checks begin. Failed processes restart; controllers replace missing pods and remount their PVCs. GKE node auto-repair and auto-upgrade are enabled. Disks remain in `us-west1-a`: a node replacement can reattach them, but node repair causes downtime and a zone outage needs separate recovery. Retained disks are not backups; the manual GCS database/payload backup remains the recovery path for those stores. Redis disk loss requires separate restoration/reconstruction; Redis is not included in that GCS backup routine.

## Private admin

The development admin runs in `events-concierge-admin`: its frontend and API both bind to pod loopback. There is no Service; access requires Kubernetes port-forward permission. The ordinary API keeps administration disabled. Shared ingestion uses the separately enabled cadence CronJob; legacy scheduling stays disabled.

```bash
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 deployment/events-concierge-admin 14002:3000
```

With the shared kubeconfig, open http://127.0.0.1:14002/admin. The dedicated pod uses the shared live development database and current immutable images. Its readiness checks exercise the admin overview through both API and frontend. Legacy recovery access uses its separate kubeconfig and port 13001.

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
