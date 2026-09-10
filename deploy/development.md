# Private GKE development

Current setup and remaining acceptance checks: [GCP & Operations](https://app.notion.com/p/3a5d865005a881f288dfdc9993b8fdd2).

This profile uses real PostgreSQL, Redis, Temporal and GCS with explicit mock external product adapters. No real email, booking or Calendar actions. The older staging Terraform root remains separate.

## Infrastructure

Use Terraform 1.16.1, gcloud, kubectl with gke-gcloud-auth-plugin, Helm 3, Python with PyYAML, and Docker Buildx. Authenticate as `iliazlobin27@gmail.com`; target `project-9c8cce04-f94d-40fc-aa6` explicitly. Never copy local credentials to another machine.

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

## Connect and initialize

```bash
export KUBECONFIG="$PWD/.local/kubeconfig"
gcloud container clusters get-credentials ec-dev --zone=us-west1-a --dns-endpoint --project=project-9c8cce04-f94d-40fc-aa6 --account=iliazlobin27@gmail.com
kubectl create namespace events-concierge-dev --dry-run=client -o yaml | kubectl apply -f -
python3 scripts/development/bootstrap_secrets.py --project-to-kubernetes
helm upgrade --install ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev --wait --timeout 10m
helm upgrade --install ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml --wait --timeout 15m
```

Secret values are generated directly into Secret Manager. Version 1 is pinned and existing credentials are reused; rotation requires a reviewed release. Databases and Redis use separately projected Kubernetes secrets; application pods use the GKE Secret Manager CSI driver.

## Application release

**Schema 0193 cutover is not ready in this development profile.** Do not run the migration or
promotion commands below against the existing cluster until the following wiring is implemented
and verified in an isolated rehearsal:

- Provision separate non-owner operator-controller and ingestion-executor logins, Secret Manager
  references and workload access. Migration `0182` removes the old consumer login's admin authority;
  the current development admin has no operator credential mount. Never grant these roles to `ec_app`.
- Give the loopback admin its controller credential. Move both the ingestion command processor and
  catalog Temporal worker to the executor credential; the current profile lacks this complete cutover.
- Use shared GCS for catalog claim checks across both executor processes. The mock catalog runtime
  currently chooses local filesystem storage, so setting an executor flag alone is insufficient.
- Rehearse the migration and coordinated worker/application promotion with the candidate images.
  Migration `0187` has no downgrade path; recovery requires the reviewed backup/restore procedure,
  not an assumed schema downgrade or rollback to incompatible older images.

Build the backend and `web/Dockerfile` with `docker buildx build --platform linux/amd64`, using the committed revision as backend `VCS_REF`. Push to `us-west1-docker.pkg.dev/project-9c8cce04-f94d-40fc-aa6/ec-dev/`. Bind immutable registry digests:

```bash
python3 scripts/development/release_values.py --app-image "$APP_IMAGE" --web-image "$WEB_IMAGE" --revision "$BACKEND_REVISION" --output .local/release-values.yaml
helm upgrade --install events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f .local/release-values.yaml --wait --wait-for-jobs --timeout 10m
# After migration succeeds, deploy the explicitly tested development provider.
helm upgrade events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f .local/release-values.yaml --set global.releasePhase=application --set global.runtimeProviderReady=true --wait --timeout 10m
python3 scripts/development/wait_ready.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/promote_workers.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/smoke.py
kubectl -n events-concierge-dev port-forward service/events-concierge-frontend 13000:80
```

Browse http://localhost:13000. Access remains localhost-only. Recurring jobs and autoscaling are disabled. Helm readiness alone is insufficient with `maxUnavailable=1`; the explicit replica check is required. One node and one replica mean downtime during replacement and upgrades; disks remain zonal. Do not use this profile as production.

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
python3 scripts/development/backup.py backup
python3 scripts/development/backup.py verify gs://iz27-ec-dev-backups/SET_ID
```

Backup temporarily stops namespace writers, exports all three databases and current payload objects, records image/schema metadata, uploads a completion marker last, then restores replica counts. Only completed sets are candidates. Keep at least three successful sets; no automatic deletion is configured. Verification restores dumps into disposable local Docker databases and validates checksums; the live smoke test also checks workflow completion and real GCS claim checks. Use `smoke.py --repeat 12` inside the API pod for a small sustained development load test. Production disaster recovery remains separate. Never restore over the running development stores. Backups share the same project administrative boundary.
