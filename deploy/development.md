# GKE release and recovery

Application release, private operator access and recovery on shared GKE.

- **Platform:** [gcp-foundation](https://github.com/iliazlobin/gcp-foundation) owns projects, network, GKE, access VM and `shared-retain`.
- **Application:** releases, workload identities, secrets, registry, data and backups.
- **Public edge:** [GCP Gateway HTTPS and admin IAP](public-access.md); consumer accounts use [Identity Platform](../deployment/consumer-identity.md).
- **Deployment:** manual; separate approval required; [production acceptance](../docs/production-operations.md#first-release-acceptance).

## Current deployment

**Recorded snapshot: October 5, 2026, 23:24 UTC.** Recheck runtime state before operating.
[Collection and release record](https://github.com/iliazlobin/events-concierge/issues/26)
owns deployed revisions, image digests and recovery evidence; [release preparation](https://github.com/iliazlobin/events-concierge/issues/24) owns activation and acceptance work.

| Item | Recorded state |
| --- | --- |
| Target | `iz27-platform-dev/us-west1-a`; cluster `platform-dev`; namespace `events-concierge-dev` |
| Readiness | Six app Deployments, two PostgreSQL stores, Redis and four Temporal servers ready |
| Network | ClusterIP only; no Ingress; IAP and loopback access |
| Cadence | Five-minute CronJob; queues due sources |
| Profile | `development` / `discovery`; `EC_MOCK_CLOUD=true`; OIDC and Temporal TLS off |
| Identity / transport | `not_configured`; demo identity; internal plaintext |

- **Real:** PostgreSQL, Redis, Temporal, GCS, public collection; no real email, booking or Calendar actions.
- **Prepared source:** authenticated private composition, Google/optional Apple accounts, datastore TLS, Temporal mTLS and GCP Gateway/IAP. This snapshot does not establish their activation or public availability.
- **Recovery:** manual quiesced backups and disposable restore rehearsals; [freshness alerts](https://github.com/iliazlobin/events-concierge/issues/23) remain open.

### Public collection

- **Tech Week 2026:** [official SF](https://www.tech-week.com/calendar/sf) October 5–11 and [LA](https://www.tech-week.com/calendar/la) October 12–18. `tech_week_mcp` reads only the anonymous [official calendar search](https://www.tech-week.com/mcp), at most 40 × 75 events per city, 2 MB and 20 seconds per page, paced at 1.5 seconds. Totals, dates and unique IDs must reconcile before atomic publication; a changed or incomplete calendar retains the prior catalog.
- Migration `0205` registers two official sources (`tech-week-sf-2026`, `tech-week-la-2026`) and two [Luma SF](https://luma.com/sftw) / [LA](https://luma.com/latw) community calendars, all disabled and unreviewed. Owner activation uses the audited source-configuration API: 180-minute refresh, 15-day horizon, review expiry October 20, 07:00 UTC. Only the official profiles freeze October 5–19 inclusive, preserving earlier starts; Luma uses its reviewed public future cursor, identity-checked details and 30-page cap. Other sources keep rolling windows. Source admission/pacing/publication and handoff-only authority still apply.
- Review through the Sources filter and custom dates October 5–19 with **Any price**. Missing prices, end times and coordinates stay unknown; closed/invite-only registration retains its provider state and never becomes open. Official city scope is not an exact street address; events without public coordinates do not appear on the map. Distinct official event IDs remain separate; cross-publisher merging requires exact title, time and known venue agreement. Keep compatible code/schema after registration; downgrade does not delete source history.
- The five-minute cadence CronJob queues only due, reviewed sources. Luma and Meetup sources use a six-hour refresh interval; other admitted sources refresh at least daily.
- Luma Discover supplies a city listing; separately reviewed organizer calendars walk their future-event cursor. Discover alone does not contain each organizer's full program.
- Meetup city JSON-LD supplies a limited public listing. `meetup_group_ics` adds the [official public group calendar export](https://help.meetup.com/hc/en-us/articles/39237118960013-Exporting-an-event-to-your-calendar), plus identity-checked public event details. Export coverage is provider-limited; neither feed proves complete city/platform search.
- Group exports accept only explicit public dated occurrences: at most 100 events, 2 MB, one feed plus 100 detail requests, 1.5 seconds minimum pacing and a 90-day maximum horizon. No member/RSVP feeds, attendee data, login or redirect following.
- Register new groups disabled and unreviewed through the owner control plane; activate via the audited source-configuration API. Retain handoff-only mode, the exact `/GROUP/events/ical/` URL and `https://www.meetup.com` as the sole origin.
- Malformed or incomplete feeds preserve the last successful catalog. Access denial stops collection; throttling respects backoff. Keep rights-held, paused and retired sources disabled.
- Schema rollback to `0195` is blocked once group sources exist. Disable those sources and roll back compatible application code while retaining registry/history.

### Remaining release work

Symphony owns task stages; GitHub holds the engineering records below. These prerequisites are not deployment approval.

| Prerequisite | Engineering record |
| --- | --- |
| Health/freshness alerts and scheduled recovery evidence | [Observability](https://github.com/iliazlobin/events-concierge/issues/23) |
| Provider/legal-mode configuration, scoped Secrets/IAM, transport/migration rehearsal, immutable artifacts and public/browser acceptance | [Release preparation](https://github.com/iliazlobin/events-concierge/issues/24) |

Configure Google Identity Platform, the approved legal mode and trusted HTTPS. [Consumer accounts](../deployment/consumer-identity.md) defines signup, reauthentication and erasure checks. After deployment approval, suspend cadence, drain writers, verify a fresh backup, coordinate migration/rollout, then verify identity, TLS/mTLS, CSRF, isolation, discovery and worker recovery.

- **Selected composition:** [authenticated private values](#authenticated-discovery-and-encrypted-dependencies) use retained in-cluster stores and per-process credentials; [#22](https://github.com/iliazlobin/events-concierge/issues/22) records source completion. Demo development and managed Cloud SQL values are separate profiles.
- **Erasure worker:** preserve consumer identity-deletion, application cleanup and Redis session-revocation authority; never grant migration-owner access.
- **Identity and edge:** [consumer setup](../deployment/consumer-identity.md), then [controlled public activation](public-access.md#activate). Complete private readiness before publishing hostnames; changing source does not change the serving release.
- **Deferred:** independent project-loss recovery. CI and healthy pods do not prove deployed acceptance.

## Shared application landing

| Boundary | Requirement |
| --- | --- |
| App Terraform | [shared-development](../infra/terraform/environments/shared-development); no cluster/network creation |
| Input / review | Verified `platform_contract`; saved-plan check: `scripts/development/check_shared_plan.py` |
| State | Fresh app state → `iz27-platform-dev-ec-state`; [backend setup](../infra/terraform/environments/shared-development/backend.tf.example) |
| Storage | Platform-installed `shared-retain`; data chart `createStorageClass=false`, `storageClass=shared-retain` |
| Helpers | Default to shared; retired target rejected; `--target shared` remains accepted |
| Values | Authenticated release: chart defaults → private authenticated values → generated image/identity bindings → reviewed consumer/edge overrides. Demo only: development → shared overlay → generated bindings |
| Workers | Catalog/erasure active; transactional, request-start, notification and change-delivery disabled |

- Keep state/plans/credentials outside Git; never initialize the retired target's backend for this destination.
- The separate [development root](../infra/terraform/environments/development) manages resources awaiting verified retirement.
- Remove that root only after data/image/identity dependencies are resolved and its owning state is empty.
- Existing stores: skip installation below; storage changes require separate rehearsal.
- The private operator uses the dedicated `ec-dev-operator-api` GSA, bound only to `events-concierge-dev/events-concierge-operator-api` with database access only to `ec-dev-operator-database-url`. The application identity root separately grants [RBAC policy reads](operator-access.md). This root grants it no consumer database, Redis, object or project IAM access. For an existing landing, check the saved plan with `scripts/development/check_shared_plan.py --operator-prerequisite`; only this GSA and its two IAM resources may be added, with all existing resources unchanged. Apply requires separate approval; generated release values then include the operator identity.

**New stores / coordinated restoration only**

- First complete [Connect](#shared-release-and-access); verify the dedicated shared kubeconfig, expected context and Ready nodes.

```bash
kubectl create namespace events-concierge-dev --dry-run=client -o yaml | kubectl apply -f -
.venv/bin/python scripts/development/bootstrap_secrets.py --target shared --project-to-kubernetes
helm upgrade --install ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml --wait --timeout 10m
```

1. Require TCP readiness and empty application/Temporal databases before restoration.
2. Create restricted `ec_app` with its pinned password before restore; migration 0002 will not rerun.
3. Create NOLOGIN `ec_operator_viewer`, `ec_operator_controller`, `ec_ingestion_executor`; create NOINHERIT `ec_operator_aggregate_definer`.
4. Grant viewer to controller; require `ec_owner`, aggregate-definer and `temporal` owners; leave `ec_dev_*` logins for bootstrap.
5. Restore verified dumps with `pg_restore --exit-on-error` and exact payload hierarchy; preserve owners, ACLs and provenance.
6. Disable Temporal database/schema/namespace initialization; run candidate migrations/bootstrap before writers.
7. Verify counts, isolation and historical payload reads; never substitute test fixtures.

- Redis excluded: inventory sessions, admission fences and backoffs; preserve or wait out backoffs before cold start.
- Current backups hash every payload object; older payload-only sets have narrower coverage.
- After restoration, while application and Temporal writers remain absent:

```bash
.venv/bin/python scripts/development/check_store_recovery.py --target shared
```

### Shared release and access

**Connect**

- Tools: Python 3.12, gcloud, `gke-gcloud-auth-plugin`, kubectl, Helm 3, Terraform 1.16.1 and Docker Buildx.
- Dependencies: `uv sync --frozen --python 3.12`; use `.venv/bin/python` for operation helpers.
- Set `APP_GCP_ACCOUNT` privately to the approved deployment account; verify it and the explicit cloud project. Recovery helpers also enforce their approved account.
- [Platform access](https://github.com/iliazlobin/gcp-foundation#private-access): keep IAP running; export its dedicated kubeconfig.
- Required context: `gke_iz27-platform-dev_us-west1-a_platform-dev`; Ready nodes.

```bash
kubectl config current-context
kubectl get nodes
```

**Prepare candidate**

- One reviewed commit; all CI/deployment checks passing; immutable backend/frontend digests.
- Registry: `us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/`.
- Successful `main` push or explicit `main` CI dispatch retains the public `candidate-images-COMMIT` for three days after all five CI jobs pass: tested images, `SOURCE_REVISION` and `SHA256SUMS`; no deployment credentials. PR and task-branch builds retain no archive.
- CI stops at this handoff; Workload Identity Federation (WIF) publication and private rollout automation remain pending. An archive is neither backup nor deployment.
- `APP_IMAGE` / `WEB_IMAGE`: `repository@sha256:...`; `BACKEND_REVISION`: full source commit.

**Verify and publish the tested package — after publication authorization**

Set `CI_RUN` and `BACKEND_REVISION` to the successful CI run and its full `main` commit. Download into a new directory:

```bash
set -euo pipefail
test "$(gh api "repos/iliazlobin/events-concierge/actions/runs/$CI_RUN" --jq '.conclusion')" = success
test "$(gh api "repos/iliazlobin/events-concierge/actions/runs/$CI_RUN" --jq '.head_sha')" = "$BACKEND_REVISION"
test "$(gh api "repos/iliazlobin/events-concierge/actions/runs/$CI_RUN" --jq '.head_branch')" = main
test ! -e ".local/candidate-$BACKEND_REVISION"
mkdir -p ".local/candidate-$BACKEND_REVISION"
gh run download "$CI_RUN" --repo iliazlobin/events-concierge --name "candidate-images-$BACKEND_REVISION" --dir ".local/candidate-$BACKEND_REVISION"
(
  cd ".local/candidate-$BACKEND_REVISION"
  test "$(cat SOURCE_REVISION)" = "$BACKEND_REVISION"
  shasum -a 256 -c SHA256SUMS
  docker load --input images.tar.gz
)
test "$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' events-concierge:ci)" = "$BACKEND_REVISION"
test "$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' events-concierge-web:ci)" = "$BACKEND_REVISION"
```

Publish the loaded images without rebuilding. The host's existing `gcloud` Docker credential helper for `us-west1-docker.pkg.dev` uses the active account checked below. Wrong identity/project or missing write access blocks publication; do not switch accounts or broaden IAM.

```bash
set -euo pipefail
test "$(gcloud auth list --filter='status:ACTIVE' --format='value(account)')" = "${APP_GCP_ACCOUNT:?Set the approved deployment account}"
test "$(gcloud config get-value project)" = iz27-platform-dev
REGISTRY=us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev
docker tag events-concierge:ci "$REGISTRY/events-concierge:$BACKEND_REVISION"
docker tag events-concierge-web:ci "$REGISTRY/events-concierge-web:$BACKEND_REVISION"
docker push "$REGISTRY/events-concierge:$BACKEND_REVISION"
docker push "$REGISTRY/events-concierge-web:$BACKEND_REVISION"
APP_IMAGE=$(gcloud artifacts docker images describe "$REGISTRY/events-concierge:$BACKEND_REVISION" --project=iz27-platform-dev --account="$APP_GCP_ACCOUNT" --format='value(image_summary.fully_qualified_digest)')
WEB_IMAGE=$(gcloud artifacts docker images describe "$REGISTRY/events-concierge-web:$BACKEND_REVISION" --project=iz27-platform-dev --account="$APP_GCP_ACCOUNT" --format='value(image_summary.fully_qualified_digest)')
```

Record both digests, source revision and CI run in the existing [release record](https://github.com/iliazlobin/events-concierge/issues/26), then generate values below. Private rollout requires [IAP access](#shared-release-and-access) and the **Release** procedure; preserve prior digests/configuration and a compatible backup for [rollback](../docs/production-operations.md#release-and-rollback). Verify serving identity, required processes, discovery and collection after rollout.

```bash
mkdir -p .local
.venv/bin/python scripts/development/release_values.py --target shared --app-image "$APP_IMAGE" --web-image "$WEB_IMAGE" --revision "$BACKEND_REVISION" --output .local/shared-release-values.yaml
```

**Release — after authorization and rehearsal**

- These commands use the demo overlays. Authenticated private releases use the [encrypted dependency profile](#authenticated-discovery-and-encrypted-dependencies) and its explicit gates.

1. [Quiesce and verify backup](#manual-recovery) with `--hold-stopped`; preserve cadence state and original replicas.
2. Apply approved credentials/infrastructure changes; run migration with writers stopped:

```bash
helm upgrade --install events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f deploy/helm/events-concierge/values-shared-development.yaml -f .local/shared-release-values.yaml --wait --wait-for-jobs --timeout 10m
```

3. Require successful migration/login bootstrap. Failure after Alembic commits: keep writers stopped; repair/retry.
4. Restore Temporal replicas from backup manifest; require readiness before application startup.
5. Newly restored Temporal only: install with initialization disabled. Skip for an existing compatible deployment.

```bash
helm upgrade --install ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml --set server.config.persistence.datastores.default.sql.manageSchema=false --set server.config.persistence.datastores.visibility.sql.manageSchema=false --set server.config.persistence.datastores.visibility.sql.createDatabase=false --set server.config.namespaces.create=false --wait --timeout 15m
```

6. Start application; keep cadence disabled until acceptance:

```bash
helm upgrade events-concierge deploy/helm/events-concierge -n events-concierge-dev -f deploy/helm/events-concierge/values-development.yaml -f deploy/helm/events-concierge/values-shared-development.yaml -f .local/shared-release-values.yaml --set global.releasePhase=application --set global.runtimeProviderReady=true --wait --timeout 10m
.venv/bin/python scripts/development/wait_ready.py --target shared
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/promote_workers.py
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - < scripts/development/smoke.py
```

- Require six intended Deployments ready; no deferred Deployments; correct revision/digests/schema and restricted-role access. Confirm read-only Entities/Graph and reject external-profile refresh in discovery.
- Promotion requires candidate catalog pollers. Smoke covers discovery plus Temporal/GCS echo; erasure completes synthetic cleanup.
- Collection acceptance: real reviewed refresh; command → successful run → publication. Schedule success alone is insufficient.
- Compare `fn_report_catalog_source_coverage_v1()` at matching times/windows; investigate failures and freshness; exclude fixtures.
- **Read-only coverage:** set `EC_DATABASE_URL` or `EC_DATABASE_URL_FILE` for an existing prepared database, then run `make catalog-coverage` (or `python -m events_concierge.quality.catalog_coverage`). It starts no services and applies no migrations. Missing/failed publication, pagination caps and freshness older than three cadences fail the check. Totals sum source-event memberships, not unique events across sources; empty feeds, retractions and city-listing depth remain diagnostics, not proof of complete platform coverage.
- Moves: preserve source metadata/revisions and old environment until parity/recovery acceptance; never replay historical source toggles.
- `refresh_due` projection lacks flattened release fields; inspect linked command revision/digest and actual workers.
- After acceptance, restore prior cadence/replica settings; `developmentCatalog.cadenceEnabled=true` queues due work every five minutes.
- Render source values; never feed `helm get values --all` back into Helm.
- Migration `0187` has no downgrade. No schema-0180 images after schema-0193 migration; no incompatible Helm-only rollback.
- Recovery requires verified coordinated backup and compatible images; overwriting new writes needs separate approval.

**Demo access**

The recorded demo uses Mac LaunchAgents for IAP, consumer and admin forwards. These loopback links work only on the forwarding machine. Authenticated consumer/admin access follows the [public edge](public-access.md); another machine needs its own platform authentication for private operations.

Without supervised access, keep IAP running and use one forward per terminal:

```bash
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-api 14000:8000
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-frontend 14001:80
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 deployment/events-concierge-admin 14002:3000
```

- [Consumer](http://127.0.0.1:14001) · [Admin](http://127.0.0.1:14002/admin); dedicated shared kubeconfig.
- Tunnel interruption: restart platform access, verify context/nodes, restart forwards.
- Pod replacement: restart affected forwards; Service forwards stay attached to the selected pod.

**Private HTTPS — preparation only**

- Origin: `https://localhost:14443`; callback: `https://localhost:14443/auth/callback`.
- `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`; real identity, encrypted dependencies and restricted credentials still required.
- Keep frontend forward running; browser-trusted localhost certificate and protected key outside Git.
- Set absolute `EC_LOCAL_TLS_CERT` / `EC_LOCAL_TLS_KEY` paths; never bypass certificate warnings.

```bash
caddy validate --config deploy/private-access.Caddyfile --adapter caddyfile
caddy run --config deploy/private-access.Caddyfile --adapter caddyfile
```

- [Proxy](private-access.Caddyfile): loopback `127.0.0.1:14443`; preserves Host; admin listener, redirects and automatic trust installation disabled.
- No public DNS/ingress/firewall opening. Browser TLS does not encrypt datastores.
- [Google activation and deployed login/CSRF/logout checks](../docs/production-operations.md#built-in-oidc-bff-activation).

### Authenticated discovery and encrypted dependencies

- Opt-in [data TLS values](helm/events-concierge-dev-data/values-private-tls.yaml) and [Temporal TLS values](helm/temporal-private-tls.yaml).
- [Authenticated application values](helm/events-concierge/values-private-authenticated.example.yaml) use the existing stores with Google Identity Platform, verified TLS and process-specific Temporal client keys. Use this file with chart defaults, not the demo overlays. Provisioning and deployed acceptance are separate release steps.
- The private profile disables demo admin and deferred product effects. The opt-in [private operator](helm/events-concierge/README.md#private-operator) uses the isolated controller credential; its signed identity and separate edge still need release acceptance. Consumer sign-in never grants an admin role.

| Connection | Required contract |
| --- | --- |
| Application and operator PostgreSQL | Service FQDN, `sslmode=verify-full`, `PGSSLROOTCERT` pointing to a CA-only mount; preserve each process's restricted DB role |
| Temporal PostgreSQL | Service FQDN in both SQL `connectAddr` values, verified TLS and CA-only mount; restored database/schema/namespace initialization stays disabled |
| Application Redis | `rediss`, trusted CA file, required certificate verification and hostname checking; existing password authentication |
| Application to Temporal | Frontend Service FQDN, server CA and separate client certificate/key files with `clientAuth` usage |
| Temporal internode and internal frontend clients | Server certificate with `serverAuth` and `clientAuth`, trusted client CA, verified server names |

- Datastore Secrets: `ca.crt`, `tls.crt`, `tls.key`; exact Service FQDN DNS SAN; short common name.
- Temporal server SAN also includes `ec-dev-temporal-internode`.
- `ec-dev-postgres-ca-v1`: **only** `ca.crt`; never distribute PostgreSQL server keys to clients.
- Signing keys outside workloads/Git; versioned credentials per workload; no Google/OIDC secrets in operator/catalog processes.
- PostgreSQL/Redis stage keys in memory with native ownership/`0600`; rotate Secret names and roll pods.
- Redis 7 probe `--sni` does not verify hostnames; application clients must.

**Prepare one certificate generation locally**

```bash
mkdir -p .local/private-tls
.venv/bin/python scripts/development/private_tls.py --output .local/private-tls/generation-1
```

- The new directory is `0700`; files are `0600`. Keep signing keys in `authorities/`; never mount them in workloads. Recovery custody uses GCP Secret Manager in `iz27-platform-dev`: `ec-dev-postgres-ca-recovery-g1`, `ec-dev-redis-ca-recovery-g1`, `ec-dev-temporal-ca-recovery-g1`.
- Each recovery JSON contains `ca_certificate_pem`, `ca_private_key_pem`, generation, project, namespace and that issuer's public leaf inventory. Exclude leaf keys and database credentials. Pin numeric Secret versions; verify certificate/key matching and exact recovery readback before using this CA for retained data. Keep the protected custody receipt outside Git.
- Grant recovery access only to the operator; verify Secret-level and inherited IAM. Applications and workers get no recovery access. Keep payloads out of Terraform state. Set a 30-day version destruction delay and no automatic expiry; this delay does not protect whole-Secret deletion.
- `leaves/` contains eight Kubernetes Secret bundles; root `inventory.json` records fingerprints and expiry. Create these as **immutable**, versioned Secrets in `events-concierge-dev` through the verified private access context. Use `ca.crt`, `tls.crt`, `tls.key` from the matching leaf only.
- Create three CA-only immutable Secrets: `ec-dev-postgres-ca-v1`, `ec-dev-redis-ca-v1`, `ec-dev-temporal-ca-v1`. They contain only the matching `authorities/<store>/ca.crt`. No application, migration or cadence process gets a datastore server key.
- Leaves expire after 90 days; review/rotate before 14 days remain. Use `--generation 2` and a new output directory for the next version. The issuer creates fresh roots: rotate server, client and trust Secrets together during reviewed maintenance, verify rejection tests, then retire unused generations. No automatic expiry alert or renewal is provided.
- Populate new numbered database DSN versions with the existing passwords/roles, service FQDN and `sslmode=verify-full`. `PGSSLROOTCERT=/var/run/events-concierge-tls/postgres/ca.crt` is mounted for each SQL consumer, including migration/cadence. Redis DSNs use `rediss` with `ssl_ca_certs=/var/run/events-concierge-tls/redis/ca.crt`, `ssl_cert_reqs=required` and `ssl_check_hostname=true`. Never print DSNs or put them in Helm values.
- API and erasure use the consumer DB role; executor/catalog use the restricted ingestion role; cadence uses the controller role. Pin actual Secret Manager version numbers; the example's `2` is not proof that a matching version exists. Review each process's IAM access and keep Google provider OAuth secrets outside workloads.
- This profile reuses retained databases and established restricted roles. A fresh installation needs its separately reviewed role bootstrap before migration; the TLS migration Job does not create operator/ingestion credentials.
- The separate data chart supplies default-deny and explicit Pod-selector routes for stores, Temporal, DNS and metadata access. On Dataplane V2, private IP CIDRs do not admit Pod traffic. Verify both kube-dns and NodeLocal DNSCache connectivity before cutover.
- Enable the [GCP public edge](public-access.md) only after transport verification. The frontend sidecar preserves consumer route/header filtering; GFE ingress is limited to consumer/admin frontend ports. Review both charts together: Kubernetes allow rules are additive.

**Validate without cluster changes**

```bash
helm template ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml -f deploy/helm/events-concierge-dev-data/values-private-tls.yaml
helm template ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml -f deploy/helm/temporal-private-tls.yaml
EC_HELM_BINARY=helm EC_DATA_TLS_DOCKER=1 .venv/bin/python -m pytest tests/unit/test_development_data_tls.py -q
EC_HELM_BINARY=helm .venv/bin/python -m pytest tests/deployment/test_authenticated_private_runtime.py tests/deployment/test_private_certificate_issuance.py tests/deployment/test_temporal_mtls_transport.py -q
```

**Coordinated maintenance**

1. Review Secret/IAM/mounts, certificate lifetimes and recovery access. Verify SCRAM for every login; record booleans, never hashes.
2. Check CPU/memory for workloads, cadence, migration and rollout overlap. Resolve scheduling failures; no Symphony-worker tolerations.
3. Rehearse transport conversion/rollback; verify backup; stop cadence and all writers; inventory Redis state.
4. Preserve PVCs/data; never initialize restored Temporal databases. Change listeners and clients together.
5. Require SQL queries, Redis commands and actual Temporal mTLS handshake before writers.
6. Require rejection of plaintext, wrong CA/hostname and missing/untrusted client certificates; complete authenticated application acceptance.
   Verify real Google login, reload, personal-data writes, logout and rejection of the revoked session on the trusted deployed origin. Configure the provider project/client and the approved legal mode before enabling signup; browser fixtures are not IdP acceptance.
7. Rollback: stop writers; restore listener/client configuration together; retain certificates/PVCs; verify readiness and queues before resume.

Authenticated private release gates select the profile explicitly; keep cadence disabled until promotion and acceptance:

```bash
.venv/bin/python scripts/development/wait_ready.py --target shared --profile private
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - --profile private < scripts/development/promote_workers.py
```

- The private gate requires five singleton application Deployments and rejects demo, deferred or unknown Deployments. Promotion uses runtime mTLS and requires recent workflow and activity pollers for the exact candidate build. `smoke.py` remains a synthetic demo check; use the authenticated browser acceptance above.
- Public access shares one frontend Deployment and hostname. After admin activation run `wait_ready.py --target shared --profile private --operator --shared-frontend`; verify Gateway/routes, certificate, backend IAP/IAM and browser acceptance separately before DNS publication. Historical tunnel receipts remain recoverable; the chart installs no connector.
- Add `--operator --shared-frontend` to private backup creation for publicEdge. Legacy private installations with both separate operator workloads use `--operator` alone. These flags select exact inventories; they do not verify browser authentication or create an edge. Resume reads the saved inventory without extra flags.
- For an authenticated private backup, add `--hold-stopped` to the following creation command for cutover. Replace `SET_ID` with its exact completed prefix; verification uses the saved data, and interrupted recovery selects the saved profile. The demo examples below retain their default profile.

```bash
.venv/bin/python scripts/development/backup.py backup --target shared --profile private
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Cadence's 90-second deadline includes scheduling.
- Local TLS tests do not prove GKE delivery/authorization; [Temporal authorization limits](../docs/production-operations.md#temporal).

## Persistent storage and self-healing

| Store | Persistence | Limit |
| --- | --- | --- |
| App PostgreSQL | Retained 20 GiB `pd-balanced` | Zonal; manual backup |
| Temporal PostgreSQL | Retained 20 GiB disk | Zonal; coordinated workflow backup |
| Redis | Retained 10 GiB; AOF every second + RDB; `Recreate` | About one second crash loss; excluded from database/payload backup |
| Avatars | Private `iz27-platform-dev-ec-media` | No versioning, soft delete or retention; excluded from backups |

- Pod replacement preserved markers/PVC/PV bindings; not proof of disk/zone-loss recovery.
- Startup probes permit recovery; controllers restart pods; GKE repairs/upgrades nodes.
- One zone (`us-west1-a`); repair/replacement can cause downtime. Retained disks are not backups.
- Redis disk loss needs separate restoration/reconstruction.
- Avatar access: API, private admin and erasure worker; adapter validates deletion policy.
- Restore can require avatar reupload; verify upload, cross-replica read, deletion and erasure separately.

<a id="private-admin"></a>

## Demo admin

- `events-concierge-admin`: frontend/API on pod loopback; no Service; Kubernetes port-forward permission required.
- Ordinary API: administration disabled. Cadence uses separate CronJob.
- Shared live data and immutable images; readiness checks admin overview through API/frontend.
- [Open via admin forward](#shared-release-and-access).
- Authenticated public releases use the shared frontend `/admin`, separate operator API and [configured IAP/RBAC](operator-access.md). The demo Deployment is absent from the private authenticated profile.

## Manual recovery

**Backup**

1. Use shared kubeconfig; record cadence suspension state; suspend scheduled jobs.
2. Wait for unfinished Jobs and active ingestion commands; inspect admin command/run views.
3. Require no active product workflows, catalog/command leases or pending request/notification work; recheck after writers stop.

```bash
kubectl -n events-concierge-dev patch cronjob events-concierge-ingestion-cadence --type=merge -p '{"spec":{"suspend":true}}'
```

**Normal backup — restores writers automatically**

```bash
.venv/bin/python scripts/development/backup.py backup --target shared
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
.venv/bin/python scripts/development/wait_ready.py --target shared
```

**Release/cutover — keep writers stopped**

```bash
.venv/bin/python scripts/development/backup.py backup --target shared --hold-stopped
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Run either backup path only after Jobs and collection commands finish.
- Held-stopped path: run readiness only after migration and application restart; follow [release](#shared-release-and-access).
- Replace `SET_ID` with exact completed prefix printed by backup.
- **Normal backup:** stop app writers then Temporal; retain PostgreSQL/Redis; save three databases, payloads and image/schema metadata.
- **Private recovery:** close the shared frontend first and wait for its Pods to disappear; stop writers and Temporal. Restore Temporal/writers and both APIs before reopening the shared frontend after guarded exact readiness. `--operator --shared-frontend` records this inventory; legacy `--operator` retains the separate-pair ordering. Unknown/partial inventories block backup. Failures close the edge; changed ownership or failed closure needs operator recovery. Resume uses saved mode, replicas, identities and templates.
- `recovery.json`: saved/read back before stopping writers; completion marker last; normal backup restores original replicas.
- Verification: disposable Docker restore and checksums; only completed sets qualify. Recreate schema-required password-free roles only in the disposable container; preserve owners/ACLs and verify privilege and tenant isolation.
- Retain at least three successful sets; no automatic deletion; same project boundary, no independent project-loss protection.
- Excludes Redis/private avatars. Never restore over running stores.
- Restore prior cadence state after readiness/verification; `resume` restores no CronJob schedules.
- Restart forwards after pod replacement.

**Interrupted backup — before migration or rollout**

```bash
.venv/bin/python scripts/development/backup.py resume gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Restore connectivity first; exact prefix printed before quiescing.
- Repeatable; Temporal first; restores replicas only, not data/images/schema.
- Refuses unknown names, writer replicas outside 0/1 (private public edge 0/2), changed schema/Deployment identities/pod templates; private recovery also refuses a changed Deployment inventory. Investigate, never force.
- Preserve incomplete prefixes as evidence; never restore their data.
- Older sets without `recovery.json`: reviewed manual recovery from saved manifest.
- After migration starts: compatible-image/coordinated-data recovery; `resume` is not rollback.
- Optional development load: `smoke.py --repeat 12` inside API pod; not production disaster-recovery acceptance.
