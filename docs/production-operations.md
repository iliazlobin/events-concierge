# Production operations

Release, recovery and incident requirements. Current environment commands: [private runbook](../deploy/development.md).

- **Current release scope:** discovery. Full concierge capabilities remain deferred.
- **Acceptance:** configuration checks and CI do not establish production readiness.
- **Recovery targets ([NFR-13](../design/requirements.md#5-non-functional-requirements)):** RPO ≤60 seconds; no acknowledged transaction loss; serving API RTO ≤24 hours.
- **Recovery requirement:** rebuild PostgreSQL projections from relational state, Temporal histories and durable outboxes.
- **Cleanup evidence:** disabled provider ports cannot prove absence of credentials or completed cleanup, including never-connected tenants.

## First release acceptance

- Set `EC_RELEASE_PROFILE=discovery`: search, filters, Events/Map/Calendar, details and provider registration links.
- Keep chat, automated RSVP, handoffs, notifications, Calendar sync, purchases and API keys disabled.
- Existing API-key records remain preserved; key authentication is not implemented.
- `EC_MOCK_CLOUD` is independent of product scope. Mock-backed deployments remain development candidates.

| Gate | Required evidence |
| --- | --- |
| Source | Locked lint/types, unit/deployment contracts, disposable-database integration/migrations, quality/load, restore rehearsal, web tests/builds/browser checks |
| User flows | Actual Next.js + candidate API: identity, filters, views, history/pagination, provider links, loading/empty/error states, mobile/keyboard, settings, tenant isolation and erasure |
| Deferred capabilities | Direct routes rejected; no enqueue/provider call; workers and credentials disabled; retained work cannot resume external effects |
| Catalog | Reviewed sources imported; last-good data preserved; same-window non-fixture counts compared; refresh and later scheduled runs observed; no unexplained loss |
| Destination | Private endpoints, real identity/CSRF, monitoring, complete restore and compatible immutable images |

- Record failed, skipped and unavailable gates. Fixtures, imported counts, empty queues and healthy workers do not prove live behavior.
- Switch access after acceptance. Protect recovery copies before retiring compute through its owning Terraform state.
- **Platform owner:** [gcp-foundation](https://github.com/iliazlobin/gcp-foundation) — foundation, network, GKE.
- **Application owner:** namespace, workload identities/permissions, releases, data, migrations and backups.

## Runtime implementation

| Boundary | Current limit |
| --- | --- |
| Discovery | Real GCS, PostgreSQL, Redis and built-in OIDC composition; deferred providers disabled |
| Full product | Notifier, production notification-secret protector, vault/injection broker and production Calendar binding/access remain unprovisioned |
| Account erasure | Unavailable provider cleanup keeps erasure fenced and pending; no false purge receipt or final database deletion |
| Secret mounts/IAM | Shared runtime mounts require per-process isolation before least-privilege acceptance |
| Browser registration | No production browser-fleet worker; fleet, broker, egress and isolation evidence required before activation |

Source: [runtime composition](../src/events_concierge/runtime.py), [deployment validation](../src/events_concierge/operations/config_validation.py), [Helm configuration](../deploy/helm/events-concierge/README.md).

## Release and deployment order

Use one immutable Python image digest for migration, API and Python workers; record the separate Next.js digest. Supply platform-managed configuration/secrets. Never bake a populated `.env` into an image.

1. Record owner/change ticket, prior revision/digests, schema head, recoverable restore point, queue baseline and controls. Preserve kill switches/quarantines unless the incident owner approves release.
2. Build and scan locked images. Run CI, migration/integration, security-header, Temporal replay/compatibility and `make test-browser` checks. Keep real BFF/CSRF canary evidence separate from fixture tests.
3. Require one `alembic heads` result. Run `alembic upgrade head` once with migration-owner authority. Bootstrap `ec_app` through `EC_APP_ROLE_PASSWORD(_FILE)`; workloads receive only non-owner, non-`BYPASSRLS` credentials. Use the separate [rotation procedure](#application-database-role-rotation) for existing passwords.
4. Roll the required workers and bounded CronJobs. Verify queue pollers, immutable Worker Deployment builds, database connectivity and heartbeats before traffic.
5. Roll API/Next.js. Admit API traffic only after `/readyz`. Check frontend liveness, proxied readiness, assets/manifest, `/v1`/`/auth` proxying, content types and security headers. Keep FastAPI ClusterIP-only.
6. Verify the [identity contract](#built-in-oidc-bff-activation): deployment-session UI, no onboarding/local tenant-header authentication, safe return URLs and exact session/CSRF behavior.
7. For **full-product activation only**, prove preview creates no durable request; one submission produces one request/start-outbox outcome; workflows and notification ledgers converge without duplicate effects. Verify one tenant-consistent `request_outcome_links` row and bounded UI refresh. Keep these routes disabled in discovery.
8. Compare queue age, errors, latency and lifecycle invariants with baseline. Close the change after a clean observation window.

- Release does not authorize source activation, a live crawl or unbounded dispatch.
- Anonymous Meetup: one reviewed city request + at most 40 same-origin, identity-checked detail requests. No redirects, credentials, cookies, app state or member/RSVP/attendee data.
- Detail failure preserves the city candidate; city-page failure is atomic. Tenant OAuth actions are a separate capability.
- Source-specific checks: [Meetup design](../design/meetup-ingestion.md), [ingestion runbook](meetup-ingestion-runbook.md).

### Executable release evidence

```bash
# Structural example only; no usable secrets or release eligibility.
make validate-production-example

# Deployment-owned EC_ settings and RuntimePorts provider.
make validate-production

# Replace every example value with the approved candidate identity.
make staging-canary \
  BASE_URL=https://staging.concierge.example \
  EXPECTED_RELEASE_REVISION=0123456789abcdef0123456789abcdef01234567 \
  EXPECTED_IMAGE_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

| Contract | Required configuration |
| --- | --- |
| Database | Direct non-local TLS with `sslmode=verify-full`; or Cloud SQL Proxy at `127.0.0.1`, explicit port, `sslmode=disable` for the Pod-local hop |
| Redis | Shared verified `rediss`; certificate and hostname verification enabled |
| Temporal | Valid TLS + API key or explicit CA/client-certificate authentication; distinct queues and immutable Worker Deployment versioning |
| Pools/storage | Zero pool overflow; pool ≥ activity concurrency; explicit GCS project/bucket/prefix |
| Identity/provider | Approved HTTPS origin; complete callable runtime boundaries; no mock graph in staging/production |
| Process credentials | Isolated operator/controller/executor database roles; no migration-owner values or file paths in non-local application processes |

- API/consumer-worker startup enforces preflight before readiness. Catalog executors require isolated DB, verified Redis/Temporal, shared payload storage and immutable release.
- Operator API/cadence controller require isolated DB transport and release identity; no consumer, Google, Redis or Temporal credentials.
- `--structural-only`: `example_contract`, `preflight_eligible=false`, `release_eligible=false`.
- Full configuration check: `wiring_preflight`, `preflight_eligible=true`, `release_eligible=false`.
- Successful wiring cannot prove provider behavior or close external launch gates.

**Private browser origin**

- `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`.
- Exact `EC_PUBLIC_BASE_URL=https://localhost:14443`; callback `https://localhost:14443/auth/callback`.
- No alternate host, port, path or query. Default `remote_https` rejects loopback.
- Preserve trusted TLS, Secure host-only cookies and exact-Origin CSRF.
- Origin configuration does not encrypt dependencies or authorize non-mock activation. Follow [encrypted dependency preparation](../deploy/development.md#encrypted-dependency-preparation).

**Canary evidence**

- Checks liveness, database/Temporal readiness, assets/headers, identity UI, unsafe returns, tenant-header rejection, metrics and immutable release identity.
- Protected `EC_CANARY_SESSION_COOKIE`: add real session resolution and missing-CSRF rejection.
- Also supplying `EC_CANARY_CSRF_TOKEN` revokes that session through `POST /auth/logout`. Use a throwaway session only.
- Real IdP login/callback, expiry, replay and cleanup still require a field walkthrough.
- Non-local canaries require revision + digest. HTTP/degraded-Temporal exceptions require explicit `--allow-local-mode` and loopback; never send session secrets over HTTP.
- `python -m events_concierge.operations ... --output PATH` creates sanitized JSON exclusively; existing evidence is never replaced.
- Keep provenance/scan, canaries, recovery, approval and observation timestamps with the change. Individual reports remain `release_eligible=false`.

### Rollback and migration safety

- Prefer expand/contract changes; remove old schema only after old processes are gone.
- Application rollback requires compatibility with current schema and Temporal histories.
- Incompatible rollback: stop rollout, contain unsafe effects, fail forward.
- No routine Alembic downgrade or whole-database restore to undo a release.
- Replay-test workflow changes; retain prior builds until affected histories can replay or are explicitly migrated.
- No ad hoc lifecycle/lease/outbox/notification-ledger `UPDATE`/`DELETE`; use guarded repair paths.
- After controller rollback, run `make rollback-verify` with `BASE_URL`, `EXPECTED_RELEASE_REVISION` and `EXPECTED_IMAGE_DIGEST` set to the prior immutable release.
- This verifies serving identity; it does not deploy, change schema or restore data.

## Health, readiness, and engine degradation

| Endpoint/state | Meaning/action |
| --- | --- |
| `/healthz` | Process liveness only |
| `/readyz`: PostgreSQL unavailable | `503`; remove replica from service |
| `/readyz`: built-in identity Redis unavailable | `503`, `identity=unavailable`; remove replica |
| `/readyz`: database ready, Temporal unavailable | `200`, Temporal `degraded`; retain durable intake |
| `/readyz`: dependencies ready | `200`; local/non-BFF identity reports `not_configured` |
| `/versionz` | Immutable revision and OCI digest |
| `/metrics` | Prometheus readiness, build, HTTP counts and latency; network-restrict and scrape each replica |

- Invalid Temporal security configuration fails startup. Valid but unreachable engine/credentials degrade readiness.
- Temporal outage: new intake remains in `request_start_outbox`; workflows and new workflow-owned handoffs stop progressing.
- Page on engine outage and start-outbox age. Recover through deterministic reject-duplicate starts; no alternate execution path.
- Outage acceptance: [ADR-010](../decisions/adr-010-temporal-cloud-engine.md) and [restore drill](#restore-drill).
- Metrics exclude raw paths, queries, tenant IDs, headers, bodies and capabilities. They do not replace database/engine/provider monitoring.

## Required process inventory

Start only the processes required by the selected release profile. Isolate long-lived workers; scale only within lease/idempotency contracts. Run scheduled repairs/scans as one-shot CronJobs with `concurrencyPolicy: Forbid`.

| Process | Command | Responsibility |
| --- | --- | --- |
| Next.js | `node server.js` | Same-origin frontend and bounded runtime API proxy |
| API | `uvicorn events_concierge.api.app:app --no-access-log` | Tenant reads/actions, durable intake, readiness |
| Transactional Temporal worker | `EC_TEMPORAL_WORKER_ROLE=transactional python -m events_concierge.workflows.worker` | Request/registration workflows; disabled when deferred |
| Catalog Temporal worker | `EC_TEMPORAL_WORKER_ROLE=catalog python -m events_concierge.workflows.worker` | Catalog workflows/activities |
| Request-start worker | `python -m events_concierge.workers.request_starter` | Start-outbox replay; deferred request lane |
| Account-erasure worker | `python -m events_concierge.workers.account_erasure` | Fenced resumable cleanup |
| Notification worker | `python -m events_concierge.workers.notifier` | Deferred notification delivery |
| Change-delivery worker | `python -m events_concierge.workers.change_detection` | Deferred workflow signals/repair |
| Handoff-expiry repair | `python -m events_concierge.workers.handoff_expiry --once` | Bounded deferred TTL repair |
| Lifecycle scanner | `python -m events_concierge.workers.lifecycle_invariants --once` | Bounded read-only divergence scan |

- `EC_API_ORIGIN` is read at request time. Preserve same-origin cookies, redirects and edge headers.
- Proxy: untrusted forwarding/hop headers stripped; 64 KiB body cap; bounded body-read deadline.
- Consumer mutation authentication always includes CSRF. Local onboarding/tenant-header adapters are mock-only.
- Current production supports the built-in OIDC BFF. Mixed/injected identity graphs require a separate UI/revocation/canary contract before support.

### Built-in OIDC BFF activation

**Required configuration**

- `EC_OIDC_BFF_ENABLED=true`, `EC_MOCK_CLOUD=false`.
- Canonical HTTPS `EC_PUBLIC_BASE_URL`; exact `<public-origin>/auth/callback` registration.
- HTTPS issuer/authorization/token/JWKS URLs; secret-managed confidential client ID/secret; explicit asymmetric algorithms.
- `EC_UI_AUTH_START_URL=/auth/login`; token exchange uses `client_secret_basic`.
- `EC_OIDC_PROVIDER=custom_claim`: private tenant UUID claim + pre-provisioned subject mapping.

**Google ordinary sign-in**

- Set `EC_OIDC_PROVIDER=google`; leave `EC_OIDC_TENANT_CLAIM` unset.

| Setting | Exact value |
| --- | --- |
| `EC_OIDC_ISSUER` | `https://accounts.google.com` |
| `EC_OIDC_AUTHORIZATION_URL` | `https://accounts.google.com/o/oauth2/v2/auth` |
| `EC_OIDC_TOKEN_URL` | `https://oauth2.googleapis.com/token` |
| `EC_OIDC_JWKS_URL` | `https://www.googleapis.com/oauth2/v3/certs` |
| `EC_OIDC_ALGORITHMS` | `RS256` |

- Scopes: `openid email`. Accounts bind verified case-sensitive `sub`, never email/profile claims.
- Explicit provisioning: `google_subject_binding(verified_sub)` → `tenants.oidc_subject`, encoded `oidc:v1:https://accounts.google.com:<sub>`.
- Use parameter-bound `fn_provision_tenant(uuid, text, text, text)` with an approved internal UUID/contact/relay. No automatic signup, legacy rebinding or account transfer.
- Migration `0195` supplies `fn_resolve_google_tenant(text)` and its `EXECUTE` grant. Unknown/erased subjects cannot sign in; re-enrollment requires approval.
- Callback failures expose only `cancelled`, `not_authorized` or `unavailable`; no provider errors/tokens.
- Redis + lookup readiness does not prove OAuth registration or real login.
- Google reauthentication/account deletion return `503`; UI advertises `reauth_url=null`. Account selection, consent, callback time and `iat` cannot prove recent authentication.

**Private Google pilot**

- Owner acceptance of unavailable destructive-action reauthentication required.
- Canary: `python -m events_concierge.operations canary --profile private_google_pilot --base-url https://localhost:14443` plus full `--expected-release-revision` and `--expected-image-digest`.
- Supply trusted localhost CA through `SSL_CERT_FILE`; no HTTP/local-demo/degraded-Temporal overrides.
- Retain IAP + loopback access. Verify registered callback, login/cancellation/logout, CSRF, tenant isolation and replica-independent sessions.
- Pilot evidence neither authorizes activation nor establishes production eligibility.

**Session/erasure contract**

| Boundary | Requirement |
| --- | --- |
| Login | One-shot Redis transaction; state, nonce, S256 PKCE and same-origin return path; default 10-minute TTL, max 15 minutes |
| Session | `__Host-ec_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain; default 8-hour TTL, allowed 5 minutes–24 hours |
| CSRF | `__Host-ec_csrf`: Secure, SameSite=Strict, Path=/, no Domain; exact Origin + matching cookie/`X-EC-CSRF` + session digest |
| Logout | Delete Redis session before clearing cookies; outage returns `503` without false revocation; product logout does not end IdP SSO |
| Revocation | ≤32 live sessions/tenant; bounded tenant-wide revocation and permanent issuance fence |
| Custom-claim erasure | Same-session `POST /auth/reauth`; `prompt=login&max_age=0`; provider `auth_time` ≤2 minutes + bounded skew; exact `DELETE MY ACCOUNT` confirmation |
| Recent-auth grant | `EC_ACCOUNT_ERASURE_RECENT_AUTH_SECONDS`: 60–900 seconds, default 300; no session TTL extension |
| Accepted erasure | Clear cookies; underway receipt only; no authenticated completion polling after revocation |

- Require `identity=ready` before traffic. Redis: network isolation, authentication, TLS, no eviction and bounded-session capacity.
- No callback-created accounts. No provider-supplied identity ports in the current production profile.
- Tenant-effect authority holds the erasure lock until admitted provider work settles, including cancellation/overdue work.
- `EC_TENANT_EFFECT_LOCK_TIMEOUT_SECONDS`: 0.1–30, default 5. `EC_TENANT_EFFECT_TIMEOUT_SECONDS`: 0.1–60, default 30.
- Adapter transport deadline ≤ effect deadline. Alert on overdue effects; cancellation does not prove an SDK thread stopped.
- Default canary requires `auth_mode=deployment_session`, `/auth/login`, `/auth/reauth`, `/auth/logout` and matching CSRF names. Unauthenticated `POST /auth/reauth` returns `401`; Google pilot uses its explicit exception. `/v1/onboard` and local tenant-header authentication stay absent.
- Source: [OIDC/session implementation](../src/events_concierge/adapters/oidc/session.py), [erasure design](../design/system-design.md#dd6-fenced-resumable-account-erasure-across-database-and-external-systems), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [Google reauthentication limit](https://developers.google.com/identity/siwg/security-bundle#authentication_time).

**Other worker boundaries**

- Deferred request UI polling: authenticated + visible + unresolved request under six hours old; jittered 30-second base, failure backoff ≤5 minutes. Budget reads before changing cadence; polling is not notification evidence.
- Lifecycle scanner: `EC_LIFECYCLE_INVARIANT_LIVENESS_CALLS_PER_SECOND` 1–20, default 10; aggregate all replicas. After uncertain Temporal response, remaining executions are uninspectable; no inferred closure/repair.
- Catalog dispatch: `python -m events_concierge.workers.catalog_refresh_dispatcher`; source refresh: `python -m events_concierge.workers.catalog_refresh SOURCE_KEY`. Both one-shot; registry/policy/legal approval controls egress.
- Hosted operator profile: separate IAP frontend/API, signed identity, exact-Origin JSON mutations, assigned viewer/operator/reviewer roles. Consumer identity grants no operator access; `EC_ADMIN_INGESTION_ENABLED` stays local/mock-only.
- With `operator.enabled`, `ingestion_cadence --once` appends deterministic durable commands; executor owns leases/results. Legacy direct-refresh Job is rejected; Temporal Schedule cutover remains inactive.
- Operator metrics are shared database totals: aggregate replicas with **max**, never sum. Missing progress is unknown, not zero; pair backlog age with scrape age and independent worker/poller health.
- Entity refresh remains unleased; do not scale it from overview counts. Details: [ingestion administration](ingestion-admin.md#hosted-operator-boundary).

<a id="entity-profile-pacer-and-command-lease-migration-rollout"></a>

### Entity-profile, Pacer, command-lease, and ingestion-evidence migration rollout

Use `alembic heads` for the selected release, not a copied schema number. Rehearse on a production-shaped restore; measure ingestion-table scans, locks and duration.

| Upgrade boundary | Required action |
| --- | --- |
| Before `0129`/`0130` | Stop cadence/dispatch; drain old source and command workers before reconciliation |
| `0128`–`0130` | Apply in order; deploy matching API/controller/command/source workers together |
| Post-`0130` | Confirm paused runs have null completion/lease fields; old leases expire once; one reclaim; long runs renew; recheck before cadence resumes |
| `0152` entity index | If older images refreshed during migration, run the documented idempotent index rebuild after cutover |
| `0181`/`0182` operator split | Drain command plane; provision distinct non-owner logins/versioned secrets; deploy matching operator/executor images; no consumer-admin rollback |

- Legacy `Pacer wait:`, `Pacer degrade:` and `Pacer saturated:` failed forms after cutover require investigation. Provider errors merely mentioning pacing remain failures.
- Lease reconciliation is irreversible. Failed renewal cancels work without a terminal mutation; expiry and guarded reclaim choose the next owner.
- Validate old evidence as `legacy_unavailable`; shared Temporal CPU/RSS stays null. Match reviewed Meetup revisions/page limits to the adapter.
- Aggregate-role migration requires CREATEROLE + owner/grant authority, not SUPERUSER/BYPASSRLS. Full-chain Cloud SQL compatibility remains unproven.
- Exact migration contracts: [migration sources](../migrations/versions/), [ingestion execution model](ingestion-admin.md#execution-model).

## Data-store durability

### PostgreSQL

- System of record for lifecycle and queues; production requires managed Multi-AZ, encrypted storage/backups, continuous WAL/PITR and an isolated restore target.
- Measure restore-point granularity and WAL lag ≤60 seconds. Document durable commit/failover with no acknowledged-transaction loss.
- Retain base backups/WAL for the recovery window; protect keys, deletion controls, audit logs and cross-account recovery access.
- Separate migration owner; runtime role non-owner, non-superuser, non-`BYPASSRLS`.
- Alert on WAL/replication lag, storage, connections, long transactions, failed backups and restore-point age.
- Demonstrate actual RPO/RTO, RLS, queues and serving recovery in a dated drill.

### Temporal

- TLS + secret-managed API key or mTLS; intended namespace; identical claim-check converter on every client.
- Review retention, availability, throughput, archival/export and recovery under O-6.
- Private mTLS: `EC_TEMPORAL_TLS_ENABLED=true`, explicit `EC_TEMPORAL_TLS_DOMAIN`, and all three read-only files: `EC_TEMPORAL_TLS_SERVER_CA_FILE`, `EC_TEMPORAL_TLS_CLIENT_CERT_FILE`, `EC_TEMPORAL_TLS_CLIENT_KEY_FILE`. Leave `EC_TEMPORAL_API_KEY` unset.
- Validate CA, expiry, client usage and key match; SDK verifies server chain/hostname. Restart clients after file rotation.
- Self-hosted default authorization does not restrict accepted clients by namespace/API. Keep private networking and trusted workload certificates; prove unauthorized-client rejection. mTLS is not tenant isolation.
- Preserve histories while lifecycle, queues, audit or claim checks reference them.
- Separate `EC_TEMPORAL_TRANSACTIONAL_TASK_QUEUE` and `EC_TEMPORAL_CATALOG_TASK_QUEUE`; immutable Worker Deployment builds required.
- `EC_TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOW_TASKS`: 2–64, default 8. `EC_TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES`: 1–64, default 8; activity slots ≤ database pool/budget.
- `EC_TEMPORAL_RPC_TIMEOUT_SECONDS`: 0.1–60, default 5. A start timeout is not acknowledgement; preserve start-outbox replay.
- Request-start cadence/batch: `EC_REQUEST_START_POLL_SECONDS`, `EC_REQUEST_START_BATCH_SIZE`. Budget across replicas and child fanout; correct saturation rather than extending task timeout.
- Service-backed tests use randomized disposable `ec_test_*` databases only; never runtime databases or routing overrides.
- API body cap: 64 KiB. `EC_REQUEST_BODY_TIMEOUT_SECONDS`: 0.1–60, default 10; stalled mutation bodies receive `408`.

### Claim-check object storage

- Shared storage required for every referencing API/worker; local filesystem is not production storage.
- Tenant-prefix access control, encryption/TLS, immutable conditional creation, integrity checks, versioning and audit logs.
- Backup/replication/KMS recovery within PostgreSQL/Temporal RPO/RTO.
- Retention ≥ referencing histories; no age-only deletion that orphans references.
- Prove tenant-prefix erasure without cross-tenant deletion and integrity-check payloads from restored workflows.

### Redis

- Shared pacing/fairness/admission and BFF sessions; not lifecycle truth.
- Authenticated TLS Multi-AZ service; no eviction; alert on memory, failover, latency and script availability.
- Recover throttle-first; preserve browser admission recovery fence. No provider-call replay or fence weakening to clear backlog.
- Snapshot/AOF recovery reduces delay; it does not prove lifecycle RPO.

## Monitoring and alerting

Independent logs/metrics/traces and staffed alert ownership required.

| Signal | Monitor/page on |
| --- | --- |
| API/dependencies | One-minute health/readiness probes; errors/latency; database saturation; Temporal schedule-to-start, failures/non-determinism/history growth; worker restarts/missing heartbeats |
| Start outbox | Pending count and oldest age; page beyond incident budget |
| Erasure | Oldest erasing request, expired lease, attempts/failure stage, worker heartbeat |
| Deferred notifications | Ready/leased/failed rows, age, provider delivery/bounce/suppression; ADR-009 warn/page 45/90 seconds against 120-second target |
| Catalog | Cadence failure, source freshness/yield/quarantine, zero-yield shifts, stage wall times |
| Deferred lifecycle | Organizer/calendar repairs, handoff expiry, watch freshness, invariant findings |
| Controls | Global/tenant/source policy changes with actor, ticket, reason, old/new value and propagation |

- Meetup: alert on endpoint/redirect drift, malformed JSON-LD, size/cap/identity failures, yield shifts and private-member data reaching shared projections.
- Shared Temporal execution evidence omits CPU/RSS. Direct-worker resource samples are best-effort process correlations; use container/host telemetry for capacity.
- Read-only queue checks require a separately audited operations read role:

```sql
SELECT count(*) AS pending,
       min(created_at) AS oldest_created_at,
       count(*) FILTER (WHERE failed_at IS NOT NULL) AS terminal_failed
FROM outbox
WHERE delivered_at IS NULL;

SELECT count(*) AS pending,
       min(created_at) AS oldest_created_at,
       max(attempt_count) AS max_attempts
FROM request_start_outbox
WHERE started_at IS NULL;

SELECT status,
       COALESCE(last_failure_stage, 'none') AS last_failure_stage,
       count(*) AS requests,
       min(requested_at) AS oldest_requested_at,
       max(attempt_count) AS max_attempts,
       count(*) FILTER (
           WHERE status = 'erasing'
             AND lease_expires_at IS NOT NULL
             AND lease_expires_at < clock_timestamp()
       ) AS expired_leases
FROM account_erasure_requests
GROUP BY status, COALESCE(last_failure_stage, 'none')
ORDER BY status, last_failure_stage;

SELECT state, count(*), min(updated_at) AS oldest_updated_at
FROM notification_ledger
GROUP BY state
ORDER BY state;

SELECT count(*) AS pending, min(next_attempt_at) AS oldest_next_attempt_at
FROM event_change_deliveries
WHERE delivered_at IS NULL;

SELECT count(*) AS pending, min(eligible_at) AS oldest_eligible_at
FROM handoff_expiry_queue
WHERE resolved_at IS NULL;
```

- Never record payloads, tokens, email bodies, OAuth credentials or claim-check bytes in telemetry/tickets/chat.
- Redact bearer tokens in `/v1/tasks/*/done` at CDN/proxy/APM. Preserve `no-store`, `no-referrer`, inert GET and explicit completion POST.
- Legacy plaintext handoff rows quarantined by `0106` cannot be recovered by copying tokens. Recreate through the normal workflow.

## Incident controls

Use control-plane/migration-owner authority for policy changes; record ticket and prior values. Replace placeholder identifiers only after audited lookup.

```sql
-- Freeze all autonomous mutations.
BEGIN;
SELECT public.fn_set_policy_global_kill_switch(true);
COMMIT;

-- Freeze one verified tenant.
BEGIN;
SELECT public.fn_set_tenant_policy_kill_switch(
    '00000000-0000-0000-0000-000000000000'::uuid,
    true
);
COMMIT;

-- One-way quarantine of a known source; also callable by the app role.
SELECT public.fn_quarantine_source('meetup', 'ban');
```

1. Contain with the narrowest safe control; use global when scope is unknown. If propagation fails, disable egress or stop the faulty worker.
2. Verify the durable control row and pre-mutation denial. Preserve leases, histories, outboxes, objects, logs and failed rows.
3. Distinguish database outage, engine degradation, Redis recovery, provider failure, ban and credential compromise.
4. Restore dependencies → workers → API traffic. Let guarded workers reclaim leases; never force acknowledgement.
5. Prove safe read/dry run and one guarded canary before release. Tenant/global switches use the same functions with `false`.
6. Verify queue convergence, notification failures, invariants, claim-check reads and no duplicate external effects.

- Never evade source blocks with rotated identities/proxies/accounts.
- Quarantine release requires owner/legal review through `fn_set_source_policy`; preserve other policy fields and review evidence.

## Secrets and key rotation

- Secret manager/workload identity; least privilege and separate rotation for each database, cache, engine, identity, storage, provider and model credential.
- No secrets in images, dumps, workflow history, telemetry, prompts, argv or support bundles.
- Value + `*_FILE` are mutually exclusive; mounted files must be bounded absolute regular UTF-8 files. Split shared runtime mounts by process.
- Local AES-GCM key is public development scaffolding, never production protection.
- Full-product provider gaps: [runtime implementation](#runtime-implementation). KMS/SES adapters still require real IAM, notification identity, delivery/bounce/suppression and ADR-009 post-send ownership.

1. Create a second credential/key with least privilege.
2. Update secret references; roll every consumer with one immutable release/config revision.
3. Verify authentication, storage, Temporal polling, queue drain and provider canary.
4. Revoke old credentials; verify rejected use is visible; record completion.

### Application database role rotation

- `ec_app` is fixed and non-owner. Migration `0002` creates NOLOGIN without an initial password; `EC_APP_ROLE_PASSWORD(_FILE)` enables login.
- Re-running migrations does not rotate an existing password. Rotation has **no dual-password overlap**.

1. Plan maintenance; retain prior password/DSN versions. Create matching numeric secret versions for `EC_APP_ROLE_PASSWORD` and `EC_DATABASE_URL`; do not update running DSNs yet.
2. Quiesce API and every database worker. Render `global.releasePhase=role-rotation`, `jobs.roleRotation.enabled=true`; only owner URL/password files enter the maintenance Job.
3. Run `python -m events_concierge.operations rotate-app-role-password`. Require success report: `role=ec_app`, login enabled, no elevated attributes/memberships. Failure/timeout blocks rollout.
4. Deploy `global.releasePhase=application` with the new DSN version; verify readiness through `ec_app` and run the canary.
5. Retain old secrets through observation/rollback window. Rollback: run the gated Job with prior password **before** redeploying prior DSN/images. Helm values alone cannot restore authentication.

- Client parameter hiding does not protect server/proxy/audit logs; disable/redact SQL and bind-parameter logging during migration/rotation.
- KMS rotation: use rewrap procedures; no plaintext in operator shells. Retain old immutable key versions until ciphertext/backups have tested recovery paths.
- Compromise rotation also revokes active OAuth/source sessions and audits the exposure window.

## Restore drill

Before launch and at the owner-approved recurring cadence; record timestamps and measured results.

**Local rehearsal:** `make restore-drill-local` starts Compose dependencies and runs migrations; use only an authorized isolated local environment with API/writers stopped.

- Dumps/restores into random `ec_restore_drill_*`; checks schema, durable aggregates, sequences, role restrictions, FORCE RLS and two-tenant isolation.
- No workers/provider calls; temporary dump/database removed; sanitized report retained.
- Does not prove managed PITR/WAL, key recovery, Temporal replay, claim-check recovery, production isolation or RPO/RTO.

**Managed drill**

1. Select a recovery timestamp unknown to the restore operator; record last acknowledged test transaction; start RTO clock.
2. Restore PostgreSQL/PITR and matching object versions into fenced account/network with recovery-only KMS roles. No real provider/calendar/notification access.
3. Verify schema/integrity, forced RLS via non-owner role, audit/lifecycle/queue continuity and no plaintext secrets.
4. Use a compatible worker build and designated recovery/test Temporal namespace; replay representative open histories and digest-check sampled claim checks.
5. Start workers against audited test endpoints; prove start/notification deduplication, provider/calendar idempotency and lease fencing.
6. Run invariant scanner, authenticated canary and `/readyz`; stop RTO clock when serving is ready.
7. Require recovery gap ≤60 seconds, zero acknowledged-transaction loss, RTO ≤24 hours and rebuildable projections.
8. Remove restored data under retention policy; retain sanitized evidence. Track gaps; rerun before claiming NFR-13.

Also rehearse Temporal outage: durable intake → pending start-outbox → engine recovery → exactly one parent workflow/request, resumed state and no duplicate external effects.

## External launch gates

Offline tests cannot close these gates. Applicable discovery gates remain required; deferred-product gates apply before those capabilities are enabled.

**Discovery/private release**

- [ ] Verify protected review, secret scanning and required CI on the approved upstream.
- [ ] Ratify applicable [owner decisions](../design/owner-decisions.md), requirements and explicit scope deferrals.
- [ ] Provision production-shaped PostgreSQL/PITR, Redis, shared storage, secrets and OIDC; pass non-mock preflight and managed restore.
- [ ] Complete real BFF login/logout/expiry/revocation, tenant mapping, CSRF, edge, TLS and Redis acceptance. Explicitly approve Google pilot reauthentication limits where applicable.
- [ ] Approve retention/legal hold, pseudonymous tombstone/session-fence and audit policies; field-prove every enabled cleanup adapter and retained backup copy.
- [ ] Prove Temporal physical deletion within NFR-11's 72-hour window; `NOT_FOUND` alone is insufficient. Disable or separately purge archival/export copies, including late starts.
- [ ] Close O-6 engine SLA/capacity/retention/recovery/cost review; current target SLA ≥99.9%, approximately 1,600 peak transitions/second.
- [ ] Complete source-specific legal/commercial-use review and owner activation. Disabled/quarantined sources stay disabled.
- [ ] Deploy independent telemetry/canaries/alerts, worker heartbeats and on-call ownership; rehearse containment and recovery.

**Before full-product activation**

- [ ] Resolve P20 calendar recovery/tri-state semantics and ADR-009 post-send acknowledgement ownership.
- [ ] Assign authenticated owner, SLA and guarded resolution for `handoff_completion_attempts.outcome='review_required'`; no ad hoc override.
- [ ] Ratify FR-11–FR-18 implementation/deferral; D9/D10 remain held.
- [ ] Provision notifier, notification-secret protector, vault/injection broker and production Calendar binding/access; pass full provider preflight.
- [ ] Prove recent-auth erasure, real adapter cleanup and immutable Calendar inventory across missing credentials, pagination, legacy events, retries and cancellation. Establish watch create/store/stop cleanup before enabling watches.
- [ ] Complete G1 real request corpus/capacity, G2 Meetup Pro OAuth and G3 inbound-domain routing checks.
- [ ] Complete Google OAuth publishing/scope review and tenant token lifecycle; Gmail scopes remain forbidden.
- [ ] Provision distinct inbound/outbound domains, SES identity/warm-up, delivery/bounce/suppression, address verification and pager routing.
- [ ] Complete browser fleet/isolation/ZDR/egress/concurrency/injection-broker/credential-transit review.

Design authority: [ADR-004](../decisions/adr-004-data-plane-policy-killswitch.md), [ADR-007](../decisions/adr-007-db-anchored-lifecycle.md), [ADR-009](../decisions/adr-009-email-launch-notification-channel.md), [ADR-010](../decisions/adr-010-temporal-cloud-engine.md), [ADR-011](../decisions/adr-011-relay-inbox-no-gmail.md), [ADR-012](../decisions/adr-012-same-origin-static-consumer.md).
