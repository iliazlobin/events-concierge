# Production operations

Release gates, incident controls and recovery constraints. [Private GKE runbook](../deploy/development.md) owns environment state, access, rollout and backup commands.

- **Selected scope:** private discovery; self-hosted PostgreSQL, Redis and Temporal on shared GKE; GCS payload storage.
- **Pending:** authenticated private Helm composition, identity/transport activation and deployed acceptance. Implemented code, CI and healthy pods do not establish acceptance.
- **Ownership:** [gcp-foundation](https://github.com/iliazlobin/gcp-foundation) owns network/cluster/access; this repository owns application identities, releases, data, migrations and backups.

## First release acceptance

- Use `EC_RELEASE_PROFILE=discovery`. Entity graphs read the published catalog; external profile refresh stays disabled. Keep chat, RSVP, handoffs, notifications, Calendar sync, purchases and API-key authentication disabled; retained work must not resume external effects.
- `EC_MOCK_CLOUD` controls integrations independently. Mock-backed acceptance does not prove real identity/provider behavior.
- Deployment requires separate approval. Source activation/crawling also requires source-specific owner/legal approval.

| Gate | Required evidence |
| --- | --- |
| Candidate | Approved revision and immutable Python/Next.js digests; applicable CI, migrations and compatibility checks |
| Identity | Real login/logout, expiry/revocation, verified subject mapping, CSRF, TLS and cross-tenant denial; explicit Google pilot limitation below |
| Discovery | Actual Next.js/API filters, Events/Map/Calendar, entity graphs/profiles without refresh writes, details/provider links, pagination/history, empty/error/loading states, settings, mobile/keyboard |
| Catalog | Reviewed sources; same-window real counts; last-good preservation; refresh and later scheduled publication; no unexplained loss |
| Deferred actions | Direct routes rejected; no enqueue/provider effect; workers and credentials disabled |
| Operations | Private endpoints, isolated process credentials, dependency recovery, monitoring/alerts, current backup and candidate restore evidence |

Record failed/skipped/unavailable gates. Switch access only after acceptance; preserve recovery copies before retiring infrastructure through its owning state. Apply the [launch gates](#external-launch-gates) for each enabled capability.

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

- Validation establishes configuration wiring, never `release_eligible=true`. Existing managed validation is not the missing private authenticated Helm profile.
- Non-local canary requires exact revision/digest. `EC_CANARY_SESSION_COOKIE` tests session/CSRF; adding `EC_CANARY_CSRF_TOKEN` **logs out that session**. Never send session secrets over HTTP.
- `python -m events_concierge.operations ... --output PATH` creates sanitized evidence exclusively; no overwrite.
- Expand/contract schema changes; rehearse on a production-shaped restore. Drain old writers before incompatible lease, role, queue or converter changes.
- Roll back only to schema/history-compatible images; retain old workflow builds for replay. Otherwise contain effects and fail forward. No routine downgrade, whole-database restore or ad hoc lifecycle/lease/outbox edits.
- `make rollback-verify` uses the same three variables, set to the prior release. It verifies serving identity; it does not deploy or restore.

<a id="entity-profile-pacer-and-command-lease-migration-rollout"></a>
<a id="entity-profile-pacer-command-lease-and-ingestion-evidence-migration-rollout"></a>

**Migration constraints**

- `0202` joins the retained operator branch (`0198`) and published application branch (`0201`). Upgrade normally from either head; preserve applied IDs and rehearse on a restore. Never substitute a schema stamp for the missing branch.
- [Migration sources](../migrations/versions/) own version-specific reconciliation. Drain cadence/source/command workers before legacy `0128`–`0130` lease changes; reconciliation is irreversible.
- Rebuild `0152` indexes if older writers ran during migration. `0181`/`0182` require distinct operator/executor roles and matching images; no consumer-admin rollback.
- Verify renewal, expiry and one guarded reclaim before resuming cadence.
- Full-chain Cloud SQL compatibility remains unproven; aggregate-role migrations need CREATEROLE + ownership/grants, not SUPERUSER/BYPASSRLS.

## Required process inventory

[Helm values](../deploy/helm/events-concierge/values.yaml) select processes. Independent long-lived workers; bounded one-shot CronJobs use `concurrencyPolicy: Forbid`. Scale only within lease/idempotency and database budgets.

| Discovery process | Entry point / boundary |
| --- | --- |
| Next.js | `node server.js`; same-origin proxy via runtime `EC_API_ORIGIN` |
| Consumer API | `uvicorn events_concierge.api.app:app --no-access-log`; ClusterIP-only |
| Catalog cadence | `python -m events_concierge.workers.ingestion_cadence --once`; append deterministic commands |
| Ingestion-command worker | `python -m events_concierge.workers.ingestion_commands`; claim/lease commands |
| Catalog Temporal worker | `EC_TEMPORAL_WORKER_ROLE=catalog python -m events_concierge.workflows.worker` |
| Account-erasure worker | `python -m events_concierge.workers.account_erasure`; fenced cleanup and session revocation |
| Hosted operator API | Separate IAP identity/roles and database role; consumer identity grants no operator access |

- Proxy strips untrusted forwarding/hop headers; 64 KiB request cap and bounded body deadline. Secure cookies, exact-Origin CSRF and safe redirects remain required.
- Local onboarding/tenant-header identity and `EC_ADMIN_INGESTION_ENABLED` are mock-only. Production uses managed consumer accounts or the legacy OIDC BFF; injected identity requires a separate contract.
- Operator API/cadence receive no consumer, Google, Redis or Temporal credentials. Executor receives only its required DB/Redis/Temporal/storage access; no migration-owner secret mounts.
- Legacy direct-refresh Job is rejected with `operator.enabled`; Temporal Schedule cutover remains inactive. Entity refresh is unleased: no autoscaling from overview counts.
- Operator database totals aggregate with **max**, never sum. Missing progress is unknown; compare scrape age and independent worker/poller health. [Ingestion operations](ingestion-admin.md#hosted-operator-boundary).

**Deferred processes:** preserve recovery contracts; keep disabled for discovery.

| Process | Command |
| --- | --- |
| Transactional Temporal worker | `EC_TEMPORAL_WORKER_ROLE=transactional python -m events_concierge.workflows.worker` |
| Request-start worker | `python -m events_concierge.workers.request_starter` |
| Notification worker | `python -m events_concierge.workers.notifier` |
| Change-delivery worker | `python -m events_concierge.workers.change_detection` |
| Handoff repair | `python -m events_concierge.workers.handoff_expiry --once` |
| Lifecycle scanner | `python -m events_concierge.workers.lifecycle_invariants --once` |

### Consumer account activation

Use [GCP Identity Platform](../deployment/consumer-identity.md) for Google/Apple signup,
required legal acceptance or explicit owner-configured deferral, protected personal data and same-account erasure. Guests
read the published catalog. Admin access separately requires IAP with signed owner
email `iliazlobin91@gmail.com` and its configured subject/role. Deployment remains gated
on domain/provider setup, the approved legal mode and real browser acceptance.

### Built-in OIDC BFF activation

Compatibility path for provisioned accounts; self-service signup uses the managed flow above.

- Set `EC_OIDC_BFF_ENABLED=true`, `EC_MOCK_CLOUD=false`, `EC_UI_AUTH_START_URL=/auth/login`; secret-managed confidential client uses `client_secret_basic`.
- Canonical HTTPS origin; exact `<origin>/auth/callback`; HTTPS issuer/authorization/token/JWKS and asymmetric algorithm allowlist.
- Private profile: `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`, exact `EC_PUBLIC_BASE_URL=https://localhost:14443`; callback `https://localhost:14443/auth/callback`. No alternate host/port/path/query; default `remote_https` rejects loopback.
- Trusted local TLS and IAP remain required. Origin configuration does not encrypt dependencies or authorize activation; follow [transport preparation](../deploy/development.md#encrypted-dependency-preparation).

| Google setting | Required value |
| --- | --- |
| `EC_OIDC_PROVIDER` / tenant claim | `google`; leave `EC_OIDC_TENANT_CLAIM` unset |
| `EC_OIDC_ISSUER` | `https://accounts.google.com` |
| `EC_OIDC_AUTHORIZATION_URL` | `https://accounts.google.com/o/oauth2/v2/auth` |
| `EC_OIDC_TOKEN_URL` | `https://oauth2.googleapis.com/token` |
| `EC_OIDC_JWKS_URL` | `https://www.googleapis.com/oauth2/v3/certs` |
| `EC_OIDC_ALGORITHMS` / scopes | `RS256` / `openid email` |

- Bind verified case-sensitive `sub`, never email: `google_subject_binding(verified_sub)` → `oidc:v1:https://accounts.google.com:<sub>` in `tenants.oidc_subject`.
- Provision through parameter-bound `fn_provision_tenant(uuid, text, text, text)` with approved UUID/contact/relay. Migration `0195` supplies resolver/grant. No automatic signup, rebinding or transfer; unknown/erased subjects fail closed; re-enrollment needs approval.
- **Google limit:** reauthentication/account deletion return `503`; UI exposes `reauth_url=null`. Consent, account selection, callback time and `iat` are not recent-auth proof. Private pilot needs explicit owner acceptance of unavailable self-service deletion.
- Pilot canary: `python -m events_concierge.operations canary --profile private_google_pilot --base-url https://localhost:14443`, plus `--expected-release-revision` and `--expected-image-digest`; trusted CA through `SSL_CERT_FILE`. No HTTP/local-demo/degraded-Temporal overrides.
- Readiness/canary does not prove real callback registration, login/cancellation/logout, expiry, replay or replica-independent sessions; verify those against the IdP.

| Session boundary | Required behavior |
| --- | --- |
| Login | One-shot Redis state/nonce/S256 PKCE; same-origin return; transaction TTL default 10 minutes, max 15 |
| Session | `__Host-ec_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain; TTL 5 minutes–24 hours, default 8 hours |
| CSRF | `__Host-ec_csrf`: Secure, SameSite=Strict, Path=/, no Domain; exact Origin + cookie/`X-EC-CSRF` match + session digest |
| Logout/revocation | Delete Redis session before cookies; outage `503`, no false revocation; ≤32 live sessions/tenant and permanent issuance fence; not IdP logout |
| Custom-claim identity | Explicit tenant UUID claim + pre-provisioned subject; no callback-created accounts |
| Custom-claim erasure | Same-session `POST /auth/reauth`, `prompt=login&max_age=0`, provider `auth_time` ≤2 minutes + bounded skew; exact `DELETE MY ACCOUNT` |
| Recent authentication | `EC_ACCOUNT_ERASURE_RECENT_AUTH_SECONDS` 60–900, default 300; no session extension |
| Accepted erasure | Clear cookies; underway receipt only; no authenticated completion polling |

- Require `identity=ready`; Redis authenticated/TLS, isolated, no eviction, bounded capacity. Callback errors expose only `cancelled`, `not_authorized`, `unavailable`; no tokens/provider detail.
- Keep erasure lock until admitted effects settle, including overdue/cancelled SDK work. Adapter deadline ≤ effect deadline; alert on overrun. Cancellation alone cannot prove termination.
- `EC_TENANT_EFFECT_LOCK_TIMEOUT_SECONDS`: 0.1–30, default 5; `EC_TENANT_EFFECT_TIMEOUT_SECONDS`: 0.1–60, default 30.
- Default canary requires deployment-session identity, login/reauth/logout routes, matching CSRF and unauthenticated reauth `401`; Google pilot is the explicit exception. Local onboarding/tenant-header authentication stays absent.
- Source: [OIDC/session implementation](../src/events_concierge/adapters/oidc/session.py), [erasure design](../design/system-design.md#account-erasure), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [Google reauthentication limit](https://developers.google.com/identity/siwg/security-bundle#authentication_time).

## Durability and health

| Component | Operational constraint |
| --- | --- |
| PostgreSQL | Lifecycle/queue truth; runtime roles non-owner/non-superuser/non-BYPASSRLS; separate migration owner; protected encrypted backups and recovery keys |
| Payload storage | Shared tenant-prefixed storage; TLS/encryption, immutable conditional writes, integrity/versioning/audits; retention ≥ referencing histories; no age-only orphan deletion |
| Redis | Sessions/pacing/admission, not lifecycle truth; authenticated verified TLS/no eviction; restore throttle-first without weakening admission fences or replaying provider effects |
| Transport | Direct PostgreSQL `sslmode=verify-full`; or Cloud SQL Proxy at `127.0.0.1`, explicit port, `sslmode=disable` only for the Pod-local hop; verified `rediss`; explicit GCS project/bucket/prefix |
| Current recovery | [Private backup/restore](../deploy/development.md#manual-recovery); Redis excluded from database/payload backups; preserve sessions/fences/backoffs separately |

### Temporal

- TLS + API key **or** mTLS; intended namespace and identical claim-check converter. Separate transactional/catalog queues, immutable Worker Deployment builds; retain referenced histories and payloads.
- Private mTLS: `EC_TEMPORAL_TLS_ENABLED=true`, explicit `EC_TEMPORAL_TLS_DOMAIN`, read-only `EC_TEMPORAL_TLS_SERVER_CA_FILE`, `EC_TEMPORAL_TLS_CLIENT_CERT_FILE`, `EC_TEMPORAL_TLS_CLIENT_KEY_FILE`; unset `EC_TEMPORAL_API_KEY`. Verify CA/expiry/client usage/key match and server hostname; restart clients after rotation.
- Default self-hosted authorization does not restrict accepted clients by namespace/API. Private network/trusted workload certificates and unauthorized-client rejection are required; mTLS is not tenant isolation.
- Activities ≤ DB pool/budget; zero pool overflow. Workflow slots 2–64, activity slots 1–64; both default 8. RPC deadline 0.1–60 seconds, default 5; start timeout is not acknowledgement.
- Deferred intake uses start-outbox replay and deterministic reject-duplicate starts; no alternate execution during engine outage. Budget request-start cadence/batches across replicas and child fanout. [Engine decision](../decisions/adr-010-temporal-cloud-engine.md).

<a id="health-readiness-and-engine-degradation"></a>

| Probe/state | Meaning |
| --- | --- |
| `/healthz` | Process only |
| `/readyz` | PostgreSQL or enabled identity Redis unavailable: `503`; DB ready + Temporal unavailable: `200` degraded; invalid security config blocks startup |
| `/versionz` | Serving revision/digest |
| `/metrics` | Network-restricted per-replica HTTP/build/readiness metrics; not provider/engine acceptance |

- Independent probes/alerts: one-minute health, errors/latency, DB capacity/lag/backup age, Redis memory/failover, Temporal backlog/non-determinism/history growth, worker heartbeats.
- Catalog: source freshness/yield/quarantine, zero-yield shifts, cadence/stage failures, endpoint/redirect drift and private-data leakage. Shared Temporal CPU/RSS is unavailable; use host/container telemetry.
- Erasure: oldest request, expired lease, attempts/failure stage; unavailable cleanup remains fenced/pending, never a false receipt/final deletion. Disabled ports do not prove no credentials, even for never-connected tenants.
- Deferred lanes: start-outbox age, notification backlog/delivery/bounce/suppression (ADR-009 warn/page 45/90 seconds against 120-second target), repairs/handoffs/watch freshness/invariants.
- Audit controls with actor/ticket/reason/old/new values. No payloads, tokens, tenant IDs or raw URLs in metrics; no secrets, email bodies or claim-check bytes in logs/tickets.
- Redact `/v1/tasks/*/done` capabilities at edge/APM; retain `no-store`, `no-referrer`, inert GET and explicit POST. Quarantined plaintext handoffs must be recreated through normal workflow, never copied.

## Incident controls

Use authorized control-plane/owner credentials; record ticket/prior values. Replace tenant placeholder only after audited lookup.

```sql
BEGIN;
SELECT public.fn_set_policy_global_kill_switch(true);
COMMIT;
BEGIN;
SELECT public.fn_set_tenant_policy_kill_switch('00000000-0000-0000-0000-000000000000'::uuid, true);
COMMIT;
SELECT public.fn_quarantine_source('meetup', 'ban');
```

1. Contain narrowly; global switch if scope unknown. Failed propagation: stop faulty worker/egress. Verify durable control and pre-mutation denial; preserve leases/outboxes/histories/objects/logs.
2. Restore dependencies → workers → API; guarded reclaim only, no forced acknowledgement.
3. Prove safe read + guarded canary before releasing tenant/global switch with `false`; verify convergence/invariants and no duplicate effects.

Quarantine release requires owner/legal review through `fn_set_source_policy`, preserving other fields. Never evade blocks with rotated accounts/proxies. [Policy controls](../decisions/adr-004-data-plane-policy-killswitch.md).

## Secrets and key rotation

- Secret manager/workload identity; per-process mounts/IAM; no secrets in images, history, telemetry, prompts, argv or bundles. Value and `*_FILE` are mutually exclusive; files bounded, absolute, regular UTF-8.
- Create replacement → roll every consumer → verify access/queues/provider canary → revoke old credential. Compromise: revoke affected OAuth/source sessions and audit exposure.
- Local AES-GCM scaffolding is not production protection. KMS rewrap without shell plaintext; retain immutable old keys until ciphertext/backup recovery is verified.

### Application database role rotation

`EC_APP_ROLE_PASSWORD(_FILE)` bootstraps fixed non-owner `ec_app`; rerunning migrations does not rotate an existing password. **No dual-password overlap.**

1. Retain prior password/DSN; create matching numeric secret versions for `EC_APP_ROLE_PASSWORD` and `EC_DATABASE_URL`.
2. Quiesce API/all DB workers. Set `global.releasePhase=role-rotation`, `jobs.roleRotation.enabled=true`; owner files enter only the maintenance Job.
3. Run `python -m events_concierge.operations rotate-app-role-password`; require `ec_app`, login enabled, no elevated attributes/memberships. Failure/timeout blocks rollout.
4. Deploy `global.releasePhase=application` with new DSN; verify readiness/canary. Retain old secrets through observation.
5. Rollback: gated Job restores prior password **before** prior DSN/images. Helm values alone cannot restore authentication.

Redact server/proxy/audit SQL and bind parameters during rotation; client parameter hiding is insufficient.

## Restore drill

- Private recovery commands/evidence: [backup and restore](../deploy/development.md#manual-recovery). Repeat for the candidate; earlier demo restoration is not authenticated-candidate acceptance.
- `make restore-drill-local` **starts Compose and migrates**; authorized isolated environment only, writers stopped. Uses disposable `ec_restore_drill_*`, verifies schema/aggregates/sequences/roles/FORCE RLS/two-tenant isolation, removes temporary dump/database; no provider workers.
- Local drill does not prove PITR, key/Temporal/payload recovery or production RPO/RTO.

1. Record last acknowledged transaction/recovery timestamp; start clock. Restore DB and matching payload versions in a fenced environment with recovery-only keys and no real provider access.
2. Verify integrity, non-owner RLS, queue/audit continuity and no secrets. Replay representative histories with compatible builds; digest-check claim checks.
3. Start guarded workers against test endpoints; prove leases/idempotency, authenticated canary and readiness. Stop clock at serving recovery.
4. Record gaps, remove restored data under retention policy; preserve sanitized evidence. Deferred full-runtime drill also proves engine outage → durable pending intake → one resumed parent/request without duplicate effects.

**Production target, not a private-stack claim:** [NFR-13](../design/requirements.md#5-non-functional-requirements) requires RPO ≤60 seconds, zero acknowledged-transaction loss, serving API RTO ≤24 hours and rebuildable projections. Production topology/PITR, key/history/payload recovery and recurring drill evidence remain separate acceptance work.

## External launch gates

Required evidence before enabling the corresponding release or capability. Offline tests do not establish these outcomes.

**Private discovery**

- [ ] Protected upstream review, secret scanning and required CI; applicable [owner decisions](../design/owner-decisions.md) and explicit scope deferrals.
- [ ] Authenticated private Helm profile, per-process credentials/IAM, datastore TLS/mTLS, combined migration/rollback rehearsal and approved rollout; [remaining release work](../deploy/development.md#remaining-release-work).
- [ ] Separate Google client/subject mapping, trusted HTTPS, real login/logout/expiry/revocation/CSRF/isolation; explicit decision on unavailable Google self-service deletion.
- [ ] Candidate backup/restore, independent telemetry/canaries/alerts/heartbeats, on-call ownership and scheduled recovery; containment/recovery drill.
- [ ] Source-specific legal/commercial review and owner activation; disabled/quarantined sources remain disabled.

**Before production or full-product activation, as applicable**

- [ ] Production durability/availability and [O-6 engine capacity/retention/recovery/cost](../design/owner-decisions.md); managed-service and historical full-concierge sizing assumptions are not the selected private topology.
- [ ] Retention/legal hold, tombstone/session-fence/audit policies and cleanup of enabled providers/backups. Temporal physical deletion within NFR-11's 72 hours; `NOT_FOUND` is insufficient; account for archives/exports and late starts.
- [ ] Ratify FR-11–FR-18; D9/D10 remain held. Resolve P20 Calendar recovery/tri-state semantics and [ADR-009 post-send ownership](../decisions/adr-009-email-launch-notification-channel.md).
- [ ] Provision notifier, notification-secret protector, vault/injection broker and production Calendar binding/access; no false completion when cleanup is unavailable.
- [ ] Bounded activity retries/schedule-to-close and long-activity heartbeats; production `confirmation_received` caller, live provider change detection, Calendar webhook and delivery-event ingestion.
- [ ] Real recent-auth erasure and immutable Calendar inventory across missing credentials, pagination, legacy events, retries/cancellation; watch create/store/stop cleanup before activation.
- [ ] Authenticated owner/SLA/guarded resolution for `handoff_completion_attempts.outcome='review_required'`; no ad hoc override. Full submission must converge to one request/start-outbox/outcome link without duplicate effects; preview creates no durable request.
- [ ] G1 real request corpus/capacity, G2 Meetup Pro OAuth, G3 inbound-domain routing; Google OAuth/scopes and tenant token lifecycle. Gmail scopes remain forbidden.
- [ ] Distinct inbound/outbound domains, SES identity/warm-up, address verification, delivery/bounce/suppression and pager routing.
- [ ] Browser fleet/isolation/ZDR/egress/concurrency/injection-broker/credential-transit review; no production browser fleet currently provisioned.
