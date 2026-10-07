# Release

Approved artifacts, acceptance gates and application rollout. [Access](access.md) owns connection setup; [recovery](recovery.md) owns backups/restoration. Read the [release record](https://github.com/iliazlobin/events-concierge/issues/26) for serving revisions and acceptance evidence.

## First release acceptance

- Use `EC_RELEASE_PROFILE=discovery`. Entity graphs read the published catalog; external profile refresh stays disabled. Keep chat, RSVP, handoffs, notifications, Calendar sync, purchases and API-key authentication disabled; retained work must not resume external effects.
- `EC_MOCK_CLOUD` controls integrations independently. Mock-backed acceptance does not prove real identity/provider behavior.
- Deployment requires separate approval. Source activation/crawling also requires source-specific owner/legal approval.

| Gate | Required evidence |
| --- | --- |
| Candidate | Approved revision and immutable Python/Next.js digests; applicable CI, migrations and compatibility checks |
| Identity | Real signup/login/logout, expiry/revocation, verified account binding, CSRF, TLS, cross-tenant denial and same-account erasure; legacy OIDC pilots require the [legacy OIDC pilot exception](consumer-identity.md#built-in-oidc-bff-activation) |
| Discovery | Actual Next.js/API filters, Events/Map/Calendar, entity graphs/profiles without refresh writes, details/provider links, pagination/history, empty/error/loading states, settings, mobile/keyboard |
| Catalog | Reviewed sources; same-window real counts; last-good preservation; refresh and later scheduled publication; no unexplained loss |
| Deferred actions | Direct routes rejected; no enqueue/provider effect; workers and credentials disabled |
| Operations | Private APIs/stores, scoped public routes/IAP, isolated process credentials, dependency recovery, monitoring/alerts, current backup and candidate restore evidence |

Record failed/skipped/unavailable gates. Complete private readiness before approved [first-hostname publication](public-access.md#activate); verify the real HTTPS/browser flow before accepting the release. Failed or unavailable acceptance disables the dedicated public routes. Preserve recovery copies before retiring infrastructure through its owning state. Apply the [launch gates](#external-launch-gates) for each enabled capability.

<a id="rollback-and-migration-safety"></a>

## Release and rollback

1. Record owner/approval, previous digests/configuration, schema head, recoverable backup and queue/control baseline. Preserve kill switches/quarantines.
2. Build/scan locked images: one Python digest for migration/API/workers; separate Next.js digest. No populated `.env` in images.
3. Require one `alembic heads` result. An authorized migration owner runs `alembic upgrade head` once; application processes receive no owner credentials or credential paths.
4. Roll required workers; verify pollers, matching Worker Deployment builds, database access and heartbeats. Then roll API/Next.js; require `/readyz`, assets, proxying and security headers before traffic.
5. Run candidate canary and real identity/discovery walkthrough; compare errors, latency, queues and invariants through the observation window.

```bash
# Structural check only; no secrets or release eligibility.
make validate-production-example
# Deployment-owned EC_ settings and runtime provider.
make validate-production
# Set approved candidate values; use a throwaway session if testing logout.
make staging-canary BASE_URL="$BASE_URL" \
  EXPECTED_RELEASE_REVISION="$EXPECTED_RELEASE_REVISION" \
  EXPECTED_IMAGE_DIGEST="$EXPECTED_IMAGE_DIGEST"
```

- Validation establishes configuration wiring, never `release_eligible=true`. Select the [private authenticated profile](transport.md#authenticated-discovery-and-encrypted-dependencies) for retained in-cluster stores; managed Cloud SQL validation does not verify that deployment.
- Non-local canary requires exact revision/digest. `EC_CANARY_SESSION_COOKIE` tests session/CSRF; adding `EC_CANARY_CSRF_TOKEN` **logs out that session**. Never send session secrets over HTTP.
- `python -m events_concierge.operations ... --output PATH` creates sanitized evidence exclusively; no overwrite.
- Expand/contract schema changes; rehearse on a production-shaped restore. Drain old writers before incompatible lease, role, queue or converter changes.
- Roll back only to schema/history-compatible images; retain old workflow builds for replay. Otherwise contain effects and fail forward. No routine downgrade, whole-database restore or ad hoc lifecycle/lease/outbox edits.
- `make rollback-verify` uses the same three variables, set to the prior release. It verifies serving identity; it does not deploy or restore.

<a id="entity-profile-pacer-and-command-lease-migration-rollout"></a>
<a id="entity-profile-pacer-command-lease-and-ingestion-evidence-migration-rollout"></a>

**Migration constraints**

- `0211` repairs explicit catalog date eligibility independently of `0209` Muse storage and
  `0210` catalog name suggestions. For a deployment that has not enabled those capabilities,
  use `alembic upgrade 0211`, retain `EC_MUSE_ENABLED=false` and
  `EC_CATALOG_NAME_SUGGESTIONS_ENABLED=false`, and verify `alembic_version = 0211` with Muse
  tables and the name-suggestion function absent. `0212` merges these branches for full-chain
  installations; `upgrade head` also applies `0209` and `0210`.
  Rehearse the selected path before deployment. The repair replaces observation functions and
  adds a recommendation capability without changing retained event data or function signatures.
  Preserve the prior definitions, owners and grants for recovery. Compatible older images can
  remain on `0211`; do not downgrade while the candidate API uses the new recommendation function.
- `0202` joins the retained operator branch (`0198`) and published application branch (`0201`). Upgrade normally from either head; preserve applied IDs and rehearse on a restore. Never substitute a schema stamp for the missing branch.
- [Migration sources](../../migrations/versions/) own version-specific reconciliation. Drain cadence/source/command workers before legacy `0128`–`0130` lease changes; reconciliation is irreversible.
- Rebuild `0152` indexes if older writers ran during migration. `0181`/`0182` require distinct operator/executor roles and matching images; no consumer-admin rollback.
- Verify renewal, expiry and one guarded reclaim before resuming cadence.
- Full-chain Cloud SQL compatibility remains unproven; aggregate-role migrations need CREATEROLE + ownership/grants, not SUPERUSER/BYPASSRLS.
- Erasure workers need consumer identity-deletion, application cleanup and session-revocation authority, never migration-owner access.
- Independent project-loss recovery is deferred. CI and healthy Pods do not prove deployed acceptance.
- Configure the approved legal mode, then coordinate transport/migration and [consumer identity](consumer-identity.md) before [public activation](public-access.md#activate).

## GKE release

Complete [Connect](access.md#connect) first. These commands change the approved application target; deployment authorization and a rehearsed recovery path are required.

### Prepare candidate

- One reviewed commit; all CI/deployment checks passing; immutable backend/frontend digests.
- [Dockerfile](../../Dockerfile) locks dependencies and runs as non-root UID/GID `10001`. Inject credentials at deployment; never commit populated environment files.
- Registry: `us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/`.
- Successful `main` push or explicit `main` CI dispatch retains the public `candidate-images-COMMIT` for three days after all five CI jobs pass: tested images, `SOURCE_REVISION` and `SHA256SUMS`; no deployment credentials. PR and task-branch builds retain no archive.
- CI stops at this handoff; Workload Identity Federation (WIF) publication and private rollout automation remain pending. An archive is neither backup nor deployment.
- `APP_IMAGE` / `WEB_IMAGE`: `repository@sha256:...`; `BACKEND_REVISION`: full source commit.

### Frontend-only repair

- Use a reviewed `main` image package with passing CI and applicable deployment checks. Publish its tested Next.js digest without rebuilding.
- Retain the deployed chart, backend digest/revision and complete values; change only `global.frontendImage.digest`.
- Independently compare renders: only the frontend container image may change. Preserve configuration, credentials, schema, routing, workers and cadence.
- Record chart, backend and frontend revisions separately, with old/new digests and the CI receipt. Keep existing acceptance receipts unchanged.
- Before applying, verify the recorded Helm revision, Deployment identity and old image; reject concurrent changes or a pending release.
- Verify HTTPS, anonymous search, sign-in and protected endpoints after rollout. Restore only the old frontend digest if needed; never roll back the database for this repair.

### Consumer API diagnostics repair

- Use a reviewed, CI-tested immutable API digest; no schema changes or migrations. Keep schema `0208`, operator disabled and cadence held.
- Retain the deployed `aa79f8c` chart, complete values, global backend image/revision and all other worker/store images. Keep `workloads.api.env` empty; its private profile rejects API environment overrides.
- Use an independently reviewed, persisted Helm post-renderer to change only the API container image and set its `EC_RELEASE_REVISION` and `EC_IMAGE_DIGEST` to the API's truthful build identity. An optional tested frontend repair follows the image-only exception above. Compare all 46 rendered resources and reject every other change.
- Verify the current Helm revision, exact workload identities/templates, old digests, retained state and fresh private TLS/readiness before applying; refuse concurrent changes. Record API, worker, chart and frontend provenance separately without rewriting earlier receipts.
- Allow a brief API interruption. Recheck native TLS/mTLS, anonymous search, sign-in, generic rejection responses and private counters after rollout. If necessary, restore only the prior API image/build environment and optional frontend digest; preserve schema/data, held cadence and access restrictions.

### Same-origin Firebase helpers

- The [helper rollout](consumer-identity.md#gcp-setup) changes three resources through a reviewed, persisted post-renderer: API Deployment image/build metadata/auth-domain/client ID; frontend Deployment image and `checksum/public-edge`; public-edge ConfigMap.
- Compare all 46 resources. Preserve the retained chart, TLS, schema `0208`, workers and admin/cadence holds; reject every other change.
- Register the OAuth callback before changing the runtime auth domain. Verify Chrome/Safari popup signup, cancellation and callback headers before acceptance; rollback restores the prior three-resource configuration together.

### Publish the tested package

Publication authorization is required. Set `CI_RUN` and `BACKEND_REVISION` to the successful CI run and its full `main` commit. Download into a new directory:

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

Record both digests, source revision and CI run in the existing [release record](https://github.com/iliazlobin/events-concierge/issues/26), then generate values below. Private rollout requires [IAP access](access.md#connect) and the **Release** procedure; preserve prior digests/configuration and a compatible backup for [rollback](#release-and-rollback). Verify serving identity, required processes, discovery and collection after rollout.

```bash
mkdir -p .local
.venv/bin/python scripts/development/release_values.py --target shared --app-image "$APP_IMAGE" --web-image "$WEB_IMAGE" --revision "$BACKEND_REVISION" --output .local/shared-release-values.yaml
```

### Roll out the demo profile

Requires deployment authorization and rehearsal.

- These commands use the demo overlays. Authenticated private releases use the [encrypted dependency profile](transport.md#authenticated-discovery-and-encrypted-dependencies) and its explicit gates.

1. [Quiesce and verify backup](recovery.md#manual-recovery) with `--hold-stopped`; preserve cadence state and original replicas.
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

## External launch gates

Required evidence before enabling the corresponding release or capability. Offline tests do not establish these outcomes.

**Discovery release**

- [ ] Protected upstream review, secret scanning and required CI; applicable [owner decisions](../../design/owner-decisions.md) and explicit scope deferrals.
- [ ] Configure and verify the implemented private profile: per-process credentials/IAM, datastore TLS/mTLS, combined migration/rollback rehearsal and approved rollout; [release record](https://github.com/iliazlobin/events-concierge/issues/26).
- [ ] Dedicated Google client, trusted HTTPS, real signup/login/logout/expiry/revocation/CSRF/isolation and managed same-account erasure. Only legacy OIDC pilots require an explicit exception for unavailable self-service deletion.
- [ ] Candidate backup/restore, independent telemetry/canaries/alerts/heartbeats, on-call ownership and scheduled recovery; containment/recovery drill.
- [ ] Source-specific legal/commercial review and owner activation; disabled/quarantined sources remain disabled.

**Before production or full-product activation, as applicable**

- [ ] Production durability/availability and [O-6 engine capacity/retention/recovery/cost](../../design/owner-decisions.md); managed-service and historical full-concierge sizing assumptions are not the selected private topology.
- [ ] Retention/legal hold, tombstone/session-fence/audit policies and cleanup of enabled providers/backups. Temporal physical deletion within NFR-11's 72 hours; `NOT_FOUND` is insufficient; account for archives/exports and late starts.
- [ ] Ratify FR-11–FR-18; D9/D10 remain held. Resolve P20 Calendar recovery/tri-state semantics and [ADR-009 post-send ownership](../../decisions/adr-009-email-launch-notification-channel.md).
- [ ] Provision notifier, notification-secret protector, vault/injection broker and production Calendar binding/access; no false completion when cleanup is unavailable.
- [ ] Bounded activity retries/schedule-to-close and long-activity heartbeats; production `confirmation_received` caller, live provider change detection, Calendar webhook and delivery-event ingestion.
- [ ] Real recent-auth erasure and immutable Calendar inventory across missing credentials, pagination, legacy events, retries/cancellation; watch create/store/stop cleanup before activation.
- [ ] Authenticated owner/SLA/guarded resolution for `handoff_completion_attempts.outcome='review_required'`; no ad hoc override. Full submission must converge to one request/start-outbox/outcome link without duplicate effects; preview creates no durable request.
- [ ] G1 real request corpus/capacity, G2 Meetup Pro OAuth, G3 inbound-domain routing; Google OAuth/scopes and tenant token lifecycle. Gmail scopes remain forbidden.
- [ ] Distinct inbound/outbound domains, SES identity/warm-up, address verification, delivery/bounce/suppression and pager routing.
- [ ] Browser fleet/isolation/ZDR/egress/concurrency/injection-broker/credential-transit review; no production browser fleet currently provisioned.
