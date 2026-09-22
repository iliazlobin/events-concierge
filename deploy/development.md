# Private GKE development

This runbook operates the shared `platform-dev` development deployment. Shared foundation,
networking and GKE are owned by [gcp-foundation](https://github.com/iliazlobin/gcp-foundation);
do not create a second cluster from this application root for that destination.
Product scope and release gates: [first-release acceptance](../docs/production-operations.md#first-release-acceptance).

The shared discovery deployment has passed private development acceptance. Access remains IAP
and loopback-only; production gates remain open. Use Terraform 1.16.1, gcloud, kubectl with
gke-gcloud-auth-plugin, Helm 3, Python 3.12+ and Docker Buildx. Authenticate as
`iliazlobin27@gmail.com` and target `iz27-platform-dev` explicitly. Initialize the repository with
`uv sync --frozen --python 3.12`; use `.venv/bin/python` for the operation helpers.

On a shared destination, the platform must install and verify its retained `shared-retain`
StorageClass before application stores are installed. Set `createStorageClass=false` and
`storageClass=shared-retain` on the data chart; application Helm must not own the platform class.

This profile uses real PostgreSQL, Redis, Temporal and GCS with explicit mock external product adapters. No real email, booking or Calendar actions. The older staging Terraform root remains separate.

## Current deployment

The shared cluster `platform-dev` in `iz27-platform-dev/us-west1-a` runs private discovery in
`events-concierge-dev`. Six application Deployments, both PostgreSQL StatefulSets, Redis and the
four Temporal server Deployments are ready. Services are
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

## Shared application landing

The platform owns the two projects, deployment identities, network, GKE/node pool, private access
VM and `shared-retain`. Follow its [private access and storage gates](https://github.com/iliazlobin/gcp-foundation#private-access)
before installing application resources. The app-owned [shared-development root](../infra/terraform/environments/shared-development)
owns its registry, workload identities, secrets and payload, media, backup and state buckets. It never creates a
cluster or changes shared network resources.

The separate [`development` root](../infra/terraform/environments/development) remains only to
manage resources awaiting verified retirement. It is not an operation target. Remove that root
after its data, image and identity dependencies are resolved and its owning state is empty.

Use the platform's verified `platform_contract` output as the new root's input. Review its exact
saved plan with `scripts/development/check_shared_plan.py`. Bootstrap the app root into fresh local
state, then migrate only that state to its protected `iz27-platform-dev-ec-state` bucket as described
in its [backend example](../infra/terraform/environments/shared-development/backend.tf.example).
Keep plan, credentials and state out of Git. Do not initialize the legacy backend for this destination.

All commands use the platform's separate kubeconfig and loopback IAP tunnel. The context must be
`gke_iz27-platform-dev_us-west1-a_platform-dev`; the namespace remains `events-concierge-dev`.
All operation helpers default to **shared** and reject the retired target. `--target shared` remains
accepted for existing commands. Context and resource checks run before cloud or Kubernetes changes.

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

For recovery into empty stores, restore a verified coordinated database/payload backup before
starting writers. Require empty application and Temporal databases; never restore over live data.
Before `pg_restore --exit-on-error`, create restricted `ec_app` with its pinned app-role password;
migration 0002 will not rerun after restoration. Create NOLOGIN `ec_operator_viewer`,
`ec_operator_controller`, `ec_ingestion_executor` and NOINHERIT `ec_operator_aggregate_definer`,
granting viewer to controller. Preserve original owners/ACLs: `ec_owner` and the aggregate definer
for the app, `temporal` for its databases. Let migration bootstrap create the two `ec_dev_*`
operator/executor logins from pinned secrets. Keep Temporal database, schema and namespace
initialization disabled. Copy the exact payload hierarchy without changing content keys. Run
candidate migration/bootstrap, then verify manifest aggregates, tenant isolation and historical
payload reads before resume.

Redis is outside this coordinated backup. Inventory sessions, provider backoffs and admission
fences before recovery; reconstruct or wait out outstanding backoffs before a cold start. Payload
backups inventory and hash every object. Profile media is excluded for account erasure.

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

For a release with migrations, suspend cadence and drain scheduled Jobs and collection commands
as described in [manual recovery](#manual-recovery). Keep writers stopped through migration and
bootstrap acceptance:

```bash
.venv/bin/python scripts/development/backup.py backup --hold-stopped
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID
```

A failed bootstrap can leave Alembic changes committed. Keep the application stopped, repair and
retry bootstrap, then verify roles and credentials before starting the candidate. Do not start an
older image or use Helm rollback as schema rollback. A schema-changing failure requires compatible
images and the coordinated database/payload recovery procedure above, in an empty recovery store;
`backup resume` restores replicas only and refuses a changed schema.

For subsequent releases, preserve the accepted cadence and replica settings after these backup and
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
administration. These forwards grant no public access. Backup/verify/resume use the shared
destination; use only its verified recovery prefix.

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

For collection acceptance, use `fn_report_catalog_source_coverage_v1()` with an explicit as-of
time and collection window. It excludes fixtures. Require reviewed sources to have successful
execution or an investigated failure; compare per-source future events and freshness. Imported
counts alone do not prove crawling, and summed links can count the same event more than once.

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

With the shared kubeconfig, open http://127.0.0.1:14002/admin. The dedicated pod uses the shared live development database and current immutable images. Its readiness checks exercise the admin overview through both API and frontend.

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
