# Private GKE development

This runbook operates the existing `ec-dev` development deployment. The replacement shared
foundation, networking and GKE are owned by [gcp-foundation](https://github.com/iliazlobin/gcp-foundation);
do not create a second cluster from this application root for that destination.
Product scope and release gates: [first-release acceptance](../docs/production-operations.md#first-release-acceptance).

The move is pending. Validate the shared platform and application landing configuration first;
preserve the existing stack until restored data, user workflows and crawler parity pass. The commands
below still target the existing project and must not be silently repointed to the shared project.

On a shared destination, the platform must install and verify its retained `shared-retain`
StorageClass before application stores are installed. Set `createStorageClass=false` and
`storageClass=shared-retain` on the data chart; application Helm must not own the platform class.
Existing `ec-dev-retain` and its bound legacy volumes keep their current ownership.

This profile uses real PostgreSQL, Redis, Temporal and GCS with explicit mock external product adapters. No real email, booking or Calendar actions. The older staging Terraform root remains separate.

## Infrastructure

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
python3 scripts/development/bootstrap_secrets.py --project-to-kubernetes
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
python3 scripts/development/release_values.py --app-image "$APP_IMAGE" --web-image "$WEB_IMAGE" --revision "$BACKEND_REVISION" --output .local/release-values.yaml
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

## Persistent storage and self-healing

Pending deployment: Redis disk persistence and recovery-probe changes are prepared in this branch but have not been applied or rehearsed. The existing Redis deployment remains ephemeral until that rollout completes.

Application PostgreSQL and Temporal PostgreSQL each mount a retained 20 GiB GCP `pd-balanced` disk. The prepared Redis configuration mounts a retained 10 GiB disk at `/data`, with AOF synced every second and periodic RDB snapshots. Redis can lose roughly the last second of writes in a crash; persistence does not make it highly available. Its single-replica Deployment uses `Recreate` so updates stop the old writer before starting the replacement.

Startup probes allow database recovery before liveness checks begin. Failed processes restart; controllers replace missing pods and remount their PVCs. GKE node auto-repair and auto-upgrade are enabled. Disks remain in `us-west1-a`: a node replacement can reattach them, but node repair causes downtime and a zone outage needs separate recovery. Retained disks are not backups; the manual GCS database/payload backup remains the recovery path for those stores. Redis disk loss requires separate restoration/reconstruction; Redis is not included in that GCS backup routine.

## Private admin

The development admin runs in `events-concierge-admin`: its frontend and API both bind to pod loopback. There is no Service; access requires Kubernetes port-forward permission. The ordinary API keeps administration disabled. Scheduled ingestion remains disabled; operator commands are explicit actions.

```bash
kubectl -n events-concierge-dev port-forward deployment/events-concierge-admin 13001:3000
```

Open http://localhost:13001/admin. The dedicated pod uses the same live development database and current immutable images. Its readiness checks exercise the admin overview through both API and frontend.

## Manual recovery

```bash
.venv/bin/python scripts/development/backup.py backup
.venv/bin/python scripts/development/backup.py verify gs://iz27-ec-dev-backups/SET_ID
```

Backup temporarily stops application writers and Temporal while retaining PostgreSQL/Redis, exports all three databases and current payload objects, records image/schema metadata, uploads a completion marker last, then attempts to restore writer replica counts. The interruption recovery command and its limits are described in [Quiesce and back up](#quiesce-and-back-up). Only completed sets are restoration candidates. Keep at least three successful sets; no automatic deletion is configured. Verification restores dumps into disposable local Docker databases and validates checksums; the live smoke test also checks workflow completion and real GCS claim checks. Use `smoke.py --repeat 12` inside the API pod for a small sustained development load test. Production disaster recovery remains separate. Never restore over the running development stores. Backups share the same project administrative boundary.
