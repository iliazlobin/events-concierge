# Production operations runbook

This runbook describes the intended production posture. It is not evidence that the service is
production-ready. Every unchecked item in [External launch gates](#external-launch-gates), and every
restore-drill assertion below, must be closed with dated evidence before launch.

The binding recovery objectives are from
[NFR-13](../design/requirements.md#5-non-functional-requirements):

- RPO: no more than 60 seconds of committed workflow, audit, or vault state, with zero
  committed-and-acknowledged transactions lost.
- RTO: restore the serving API within 24 hours.
- PostgreSQL projections must be rebuildable from restored relational state, Temporal history, and
  durable outboxes.

These are targets, not current achievements.

## First release acceptance

The approved first release is catalog discovery, search/filters, Events/Map/Calendar, details and
provider registration links. Configure `EC_RELEASE_PROFILE=discovery`; keep the full profile for
development of deferred chat, automated RSVP, handoffs, notifications, Calendar sync, purchases
and programmatic API keys. Existing key records remain preserved; key authentication is not yet
implemented, so discovery must not advertise key creation as usable programmatic access.
The server removes deferred consumer routes before serving or generating OpenAPI. This product
boundary is independent of `EC_MOCK_CLOUD`: a private mock-backed deployment is still a development
candidate, even when it uses real catalog data.

Before accepting the candidate:

1. Pass locked lint/type checks, unit/deployment contracts, disposable-database integration and
   migration tests, quality/load tests, restore rehearsal, all web behavior tests, production builds
   and browser tests. Record failures and skipped/unavailable gates explicitly.
2. Exercise the actual Next.js application against the candidate API through localhost-only access:
   onboarding/authentication, shared filters, Events/Map/Calendar, history/pagination, provider links,
   loading/empty/error states, mobile/keyboard use, account settings, tenant isolation and erasure.
   The legacy hermetic browser suite does not establish this real-stack acceptance.
3. Verify deferred routes reject direct calls without enqueueing or contacting providers. Disable
   their workers and provider credentials in the release configuration; retained work from an older
   database must not resume external effects during migration.
4. Prove real catalog collection on the destination: import the reviewed source configuration and
   preserve last-good data, compare non-fixture per-source counts over the same collection window,
   run refreshes and observe subsequent scheduled runs. Require no unexplained loss relative to the
   local baseline. An imported count, an empty queue or healthy worker is not crawl evidence.
5. Verify private endpoints, real identity/CSRF and account isolation, monitoring, a complete data
   restore and compatible immutable images. Move access only after acceptance; retire old compute
   through its owning Terraform state after protecting the recovery copies.

Shared foundation/network/GKE belong to [gcp-foundation](https://github.com/iliazlobin/gcp-foundation).
The application owns its namespace, workload identities/permissions, releases, data, migrations and
backups. Exact current commands and relocation limits are in the [private runbook](../deploy/development.md).

## Runtime implementation

Repository-owned deployment scaffolding now includes offline-validated Terraform/OpenTofu and Helm,
native GCS claim-check storage, strict mounted-secret loading, Cloud SQL Auth Proxy-aware database
validation, explicit zero-overflow pools, secure `ec_app` bootstrap/rotation, split and versioned
Temporal workers, bounded one-shot CronJobs, and a runtime-configured Next.js proxy.

The full product profile is still not production-ready. Its GCP runtime omits notification delivery,
a production notification-secret protector, the credential vault/injection broker, and usable
production Google Calendar binding/access. The discovery profile can construct real GCS, PostgreSQL,
Redis and repository OIDC boundaries while explicitly disabling those product providers. It rejects
enabled provider overrides and configuration; full-product preflight rejects disabled ports.
Account erasure remains fenced and pending at unavailable Calendar or credential cleanup, with no
false purge receipt or final database deletion. Production acceptance requires actual cleanup
evidence, including tenants with no previously connected provider credentials; disabled cleanup
ports cannot certify their absence. Shared GCS media persistence and erasure also need deployment
verification under the dedicated bucket policy in the [private runbook](../deploy/development.md). The current chart
still mounts shared runtime secrets and relies on common runtime composition more broadly than strict
per-process IAM permits. Do not interpret a successful structural example check, Terraform/Helm
validation, or container test as authorization to deploy traffic.

## Release and deployment order

Use one immutable Python image digest for the migration Job, API, and all Python workers, plus one
separately recorded immutable Next.js image digest. Production configuration and secrets come from
the deployment platform; never copy a populated `.env` into an image.

1. Confirm an on-call owner, change ticket, rollback image digest, current database migration head,
   and a fresh recoverable database restore point. Record global, tenant, and source control state
   plus the current queue baselines. If any kill switch or quarantine is engaged, preserve it; only
   the incident owner may authorize release through an audited change.
2. Build and scan the locked image. Run lint, type checks, unit tests, migration tests, integration
   tests, static consumer-asset and security-header checks, Temporal replay/compatibility tests, and
   the committed `make test-browser` Playwright gate against that exact source revision. The
   hermetic browser suite uses deterministic product API fixtures; retain separate deployment-canary
   evidence for the real BFF/session and CSRF policy.
3. Run `alembic heads` and require exactly one head. Apply `alembic upgrade head` once, as the
   migration-owner role. A new database receives the initial `ec_app` password only from
   `EC_APP_ROLE_PASSWORD(_FILE)`; application processes receive only the non-owner,
   non-`BYPASSRLS` DSN. If an existing environment also rotates that password, keep application
   workloads quiesced, complete the separate role-rotation Job, and record its sanitized report
   before rolling out the matching new application DSN. The detailed no-overlap procedure is under
   [Application database role rotation](#application-database-role-rotation).
4. Start or roll the independently versioned transactional and catalog Temporal worker roles plus
   durable relay workers listed below. Schedule the one-shot repair/scanner CronJobs separately.
   Verify both task-queue pollers, immutable Worker Deployment builds, database connectivity, and
   worker log heartbeats before admitting new traffic.
5. Start or roll the internal API and public Next.js frontend. Keep the API out of service until its
   `/readyz` succeeds; use the frontend-local `/healthz` only for frontend liveness and its proxied
   `/readyz` for API dependency readiness. Verify `/`, Next.js assets, manifest metadata, runtime
   proxying of `/v1` and `/auth`, expected content types, cache/no-sniff handling, CSP, frame denial,
   referrer, and permissions headers. The FastAPI Service remains ClusterIP-only.
6. Query `/v1/ui-config` and require `auth_mode=deployment_session`. Prove `/v1/onboard` is absent,
   the local `X-EC-Tenant-ID` header alone cannot authenticate, unsafe return targets are rejected,
   and the built-in BFF session resolves the expected pre-provisioned tenant and OIDC subject.
   Require `auth_start_url=/auth/login`, `reauth_url=/auth/reauth`, `logout_url=/auth/logout`,
   matching CSRF cookie/header names, and no provider-supplied identity boundary in the current
   production profile. The unauthenticated canary must receive `401` from `POST /auth/reauth`,
   proving the advertised route is mounted without starting an identity transaction. Reject the
   release if any authenticated mutation accepts missing, mismatched, cross-origin, replayed,
   revoked, or expired evidence.
7. Send a synthetic authenticated preview and verify it creates no durable request. Then submit the
   durable form once, verify one deterministic request row and start-outbox result, and verify the
   workflow and notification ledgers converge without duplicate effects or a false registration
   claim. When the parent selects an outcome, require exactly one immutable, tenant-consistent
   `request_outcome_links` row and confirm Recent briefs follows that lifecycle's current state. Also
   observe one jittered pending-request refresh, confirm the dependent collections refresh once on
   changed request truth, and confirm backgrounding or resolving the request stops the timer.
8. Compare queue age, error rate, latency, and lifecycle-invariant findings with the pre-release
   baseline. Complete the change only after the observation window is clean.

Catalog refresh commands are separately scheduled, policy-gated jobs. A release must not implicitly
enable a source, run a live crawl, or turn a one-shot dispatcher into an unbounded loop.

For `meetup_city_jsonld`, release review must preserve the closed anonymous contract in
[the Meetup ingestion design](../design/meetup-ingestion.md): one exact reviewed city request plus at
most forty deterministic, same-origin, identity-checked event-detail requests; no redirects,
credential/cookie, application state, or member/RSVP/attendee ingestion. Detail failure is
best-effort and must preserve the valid city candidate with a closed reason; city-page failure is
atomic for the run.
The Meetup GraphQL OAuth action lane is a different tenant-scoped capability and must not be enabled
or treated as shared-catalog authority as a side effect of rolling out the public adapter.

### Executable release evidence

The repository-owned checks are commands, not substitutes for deployment review:

```bash
# CI/schema contract only: the example has no usable secret and the built-in GCP provider is partial.
make validate-production-example

# Real pre-deploy job: reads EC_ settings and imports the deployment-owned RuntimePorts factory.
make validate-production

# Post-deploy canary against the exact expected immutable release.
make staging-canary \
  BASE_URL=https://staging.concierge.example \
  EXPECTED_RELEASE_REVISION=0123456789abcdef0123456789abcdef01234567 \
  EXPECTED_IMAGE_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The full validator fails on a local/test environment, mock cloud graph, missing runtime provider,
HTTP/loopback public origin, process-local pacing, plaintext/loopback Redis, local PostgreSQL,
disabled Redis certificate/hostname verification, an invalid database transport profile,
plaintext/default or internally inconsistent Temporal configuration, absent Temporal Cloud
credential, a mutable release label, or an incomplete/non-callable provider boundary bundle. Direct
database TLS requires a non-local endpoint and exactly `sslmode=verify-full`; Cloud SQL Auth Proxy
mode instead requires `127.0.0.1`, an explicit port, and exactly `sslmode=disable` for the Pod-local
hop. Production also requires distinct transactional/catalog task queues, immutable Worker
Deployment versioning, an explicit GCS project/bucket/prefix, zero database-pool overflow, and a
pool at least as large as configured activity concurrency. The validator reports check names and
outcomes but never DSNs, credential values, or provider exception text.
`deployment/production.env.example` is only a credential-free structural contract; its output
is marked `mode=structural_only`, `evidence_class=example_contract`,
`preflight_eligible=false`, and `release_eligible=false`. A passing full check is marked
`evidence_class=wiring_preflight` and `preflight_eligible=true`, but remains
`release_eligible=false`: callable-surface inspection cannot prove provider behavior or close field
gates. `--structural-only` is forbidden as release evidence.

The staging canary is bounded and non-mutating. It verifies liveness; database and Temporal
readiness; consumer assets, types, and edge security headers; deployment-session UI mode; absence of
mock onboarding; rejection of an unsafe login return URL and the local tenant header; Prometheus
metric families; and the serving revision/image digest. Set `EC_CANARY_SESSION_COOKIE` only through a
protected job secret to add real session resolution and missing-CSRF rejection. For a throwaway
canary session, also set `EC_CANARY_CSRF_TOKEN`; the canary then proves CSRF acceptance by revoking
that session through `POST /auth/logout`. Neither value appears in argv or evidence. A dated
real-IdP field run must still prove login/callback, expiry, replay rejection, and cleanup; do not use
a persistent operator or customer session for the logout probe.

A non-local canary requires both the expected immutable revision and image digest. `--allow-http`
and `--allow-temporal-degraded` are rejected unless `--allow-local-mode` is also explicit; these are
loopback-only outage-rehearsal controls, never staging exceptions. Session cookies and CSRF tokens
are rejected over HTTP even in local mode. The committed staging canary enforces the current built-in
BFF routes and cookie/header contract.

`python -m events_concierge.operations ... --output PATH` writes sanitized JSON using exclusive
creation, so a rerun cannot replace prior evidence. Keep that file with the change ticket along with
image provenance/signature, vulnerability scan, provider canaries, approval, dashboard snapshots,
and the observation-window timestamps. Individual reports deliberately retain
`release_eligible=false`; the release controller and owner must combine the distinct wiring,
deployment-canary, recovery, security, field, and approval evidence. Passing repository checks alone
does not close an external launch gate.

### Rollback and migration safety

- Prefer expand/contract schema changes: deploy additive schema first, then compatible code, and
  remove old schema only in a later release after every old process is gone.
- Do not run Alembic downgrades in production as a routine rollback. Many migrations intentionally
  preserve safety changes on downgrade or cannot reverse external effects.
- Roll application processes back only when the prior image is compatible with the current schema
  and Temporal histories. Otherwise stop rollout, engage the kill switch if mutations are unsafe,
  and fail forward.
- Never restore the entire database merely to undo an application release; doing so discards newer
  committed user state. Point-in-time restore is an incident-recovery operation.
- Temporal workflow code replays against long-lived histories. A workflow change needs deterministic
  patch/version handling and replay tests before rollout. Keep the prior worker build available until
  every affected history can replay or is deliberately migrated/continued-as-new.
- Never repair lifecycle, lease, outbox, or notification-ledger rows with ad hoc `UPDATE`/`DELETE`.
  Use guarded functions and application repair paths so stale owners cannot acknowledge new work.

Before rollout, record the prior immutable revision and image digest and prove it remains available
and schema/Temporal-compatible. After the deployment controller performs an application rollback,
run `make rollback-verify` with `BASE_URL`, `EXPECTED_RELEASE_REVISION`, and
`EXPECTED_IMAGE_DIGEST` set to that prior identity. This target reruns the full staging canary and
fails if traffic still reaches a mixed or unexpected build. It deliberately does not change the
deployment or downgrade the database; those remain controller- and incident-owner actions.

## Health, readiness, and engine degradation

`GET /healthz` is shallow process liveness. It returns success while the process event loop can serve
HTTP and must not be used to assert dependency health.

`GET /readyz` is traffic readiness:

- PostgreSQL unavailable: returns `503`; remove the replica from service. Intake cannot be durably
  committed without PostgreSQL.
- Built-in OIDC BFF enabled and its bounded Redis session-store probe unavailable: returns `503`
  with `identity=unavailable`; remove the replica because it cannot safely authenticate, verify
  CSRF, or revoke sessions. Local or externally injected non-BFF auth reports `not_configured` for
  this component.
- PostgreSQL ready and Temporal reachable: returns `200`, with both components ready.
- PostgreSQL ready and Temporal unavailable: returns `200`, with Temporal reported as `degraded`.
  Keep the API serving. Request intake commits to `request_start_outbox`; the request-starter relay
  replays deterministic, reject-duplicate starts after Temporal recovers.

Insecure or internally inconsistent Temporal transport settings (for example, an API key without
TLS) fail process startup before any connection attempt. With a valid configuration, endpoint,
credential-service, or engine unreachability is an operational degradation: the API keeps the
database-backed intake path available and reports Temporal as degraded until reconnection.

During a Temporal outage, intake is durable but no new workflow-owned handoff task can be created,
and in-flight workflows do not advance. This is the bounded outage posture described by
[ADR-010](../decisions/adr-010-temporal-cloud-engine.md); it remains an owner-ratification and restore
drill gate. Page on the start-outbox age and engine outage. Do not bypass Temporal with a second,
non-idempotent execution path.

`GET /versionz` returns only the configured immutable release revision and OCI image digest.
`GET /metrics` returns Prometheus text with build identity, process start time, last database,
Temporal, and browser-identity readiness result, request totals, and duration histograms. HTTP labels
are limited to a finite method, status, and framework route template. Raw paths, query strings,
tenant IDs, headers, bodies, and handoff capability tokens are never labels. Restrict metrics at the
network/edge, scrape each API replica, and aggregate in the independent monitoring plane; the
endpoint is not an authorization boundary or a replacement for database/Temporal/provider metrics.

## Required process inventory

Run each long-lived process independently so one backlog or crash does not stop another. Run the two
scheduled entries only as one-shot CronJobs with `concurrencyPolicy: Forbid`. Scale only processes
whose lease/idempotency contract permits concurrency.

| Process | Command | Required responsibility |
|---|---|---|
| Next.js frontend | `node server.js` | Public same-origin shell plus bounded runtime proxy to the internal API; frontend liveness and proxied readiness remain distinct |
| API | `uvicorn events_concierge.api.app:app --no-access-log` | Internal tenant-scoped reads/actions, durable intake, and dependency readiness; path logging stays disabled because completion URLs carry capabilities |
| Transactional Temporal worker | `EC_TEMPORAL_WORKER_ROLE=transactional python -m events_concierge.workflows.worker` | Request/registration workflows and activities on `EC_TEMPORAL_TRANSACTIONAL_TASK_QUEUE` |
| Catalog Temporal worker | `EC_TEMPORAL_WORKER_ROLE=catalog python -m events_concierge.workflows.worker` | Catalog workflows and activities on `EC_TEMPORAL_CATALOG_TASK_QUEUE` |
| Request-start relay | `python -m events_concierge.workers.request_starter` | Replays `request_start_outbox` after API or Temporal failure |
| Account-erasure resume worker | `python -m events_concierge.workers.account_erasure` | Renews leases and resumes fenced tenant cleanup across workflow, calendar, session, vault, object-store, and database stages |
| Notification relay | `python -m events_concierge.workers.notifier` | Delivers transactional outbox rows through the notification ledger |
| Change-delivery worker | `python -m events_concierge.workers.change_detection` | Signals organizer changes and drains associated repair work |
| Handoff-expiry repair | `python -m events_concierge.workers.handoff_expiry --once` | CronJob runs one bounded repair of orphaned handoff TTL transitions after the Temporal grace period |
| Lifecycle invariant scanner | `python -m events_concierge.workers.lifecycle_invariants --once` | CronJob runs one bounded read-only lifecycle/watch/handoff/Temporal divergence scan |

The consumer web surface is a separately built Next.js image in front of the internal FastAPI
Service. Its runtime route handlers read `EC_API_ORIGIN` at request time and proxy `/v1`, `/auth`,
`/healthz`, and `/readyz` without baking a cluster service name into the image. They strip
hop-by-hop and untrusted forwarding headers, preserve cookies/same-origin redirects, cap request
bodies at 64 KiB, and apply a hard body-read deadline. Preserve that same-origin and security-header
contract through the edge. Local onboarding and the tenant-header adapter are mock-only
conveniences and must never be enabled in production. The application invokes
`CsrfProtectionPort` for every authenticated consumer mutation. Non-mock
composition can represent an injected lifecycle adapter, but the current production profile, UI,
account-erasure revocation contract, and release canary require the built-in OIDC BFF described
below. Provider-supplied identity ports must therefore be absent in that profile, and a partial or
mixed graph fails preflight. An alternate external BFF is unsupported until it has a separately
parameterized UI contract, tenant-wide session-revocation boundary, and deployment canary. Serving
the static shell or constructing a verifier without real-IdP canary evidence does not establish
production identity safety.

### Built-in OIDC BFF activation

Enable `EC_OIDC_BFF_ENABLED=true` only with `EC_MOCK_CLOUD=false`. Configure the public HTTPS origin
in `EC_PUBLIC_BASE_URL` without a path or query, and register the exact callback
`<public-origin>/auth/callback` at the IdP. Supply HTTPS issuer, authorization, token, and JWKS URLs;
the confidential client ID/secret; the private tenant UUID claim name; and an explicit asymmetric
algorithm allowlist. The current token exchange uses `client_secret_basic`. Keep the client secret
in the deployment secret manager. Set `EC_UI_AUTH_START_URL=/auth/login` for explicit production
validation; any different value that bypasses the same-origin transaction route is rejected at
configuration time.

The login transaction stores only a SHA-256-keyed opaque handle in a Secure, HttpOnly,
SameSite=Lax `__Host-ec_login` cookie and seals state, nonce, S256 PKCE verifier, and a bounded
same-origin return path in Redis for at most 15 minutes (10 minutes by default). A callback consumes
that record once before token exchange, verifies the ID token and nonce, and requires its canonical
tenant UUID plus subject to match the existing product account. It then rotates any current browser
session and issues:

- `__Host-ec_session`: opaque, Secure, HttpOnly, SameSite=Lax, Path=/, no Domain;
- `__Host-ec_csrf`: independent opaque token, Secure, SameSite=Strict, Path=/, no Domain, readable
  only so same-origin JavaScript can copy it to `X-EC-CSRF`.

Redis stores the session under the SHA-256 digest of the session handle, retains the tenant/subject
binding and only a digest—not the raw value—of the CSRF token, and applies a fixed
5-minute-to-24-hour TTL (8 hours by default). Each tenant's session set uses a SHA-256 digest of the
tenant UUID in its Redis key, prunes expired handles before admission, caps at 32 live sessions, and
is consumed in one bounded tenant-wide revocation. The permanent issuance fence is likewise
pseudonymous; its retention/legal basis remains an owner gate. Every mutation resolves the same session twice—
authentication then CSRF—and requires `Origin` to exactly equal the public origin, the CSRF
cookie/header values to match, and their digest to match that session. `POST /auth/logout` first
deletes the Redis session and only then expires all three cookies; a Redis outage returns 503 and
deliberately preserves the browser reference rather than pretending revocation succeeded. Logout
ends this product session, not the IdP's global SSO session.

Account erasure requires a separate, current-session `POST /auth/reauth` before its destructive
command. That POST passes ordinary authentication and exact-Origin/CSRF verification, then stores a
one-shot transaction bound to purpose=`account_erasure`, the current session digest, tenant,
subject, state, nonce, S256 PKCE verifier, and same-origin return path. Its authorization request
adds `prompt=login&max_age=0`; callback requires a numeric provider `auth_time` no more than two
minutes old (plus bounded clock skew), re-verifies the same still-live session/tenant/subject, and
atomically adds a server-timestamped recent-auth grant without extending the session TTL. Set
`EC_ACCOUNT_ERASURE_RECENT_AUTH_SECONDS` to 60–900 seconds (300 by default). The erasure POST also
requires the exact literal `DELETE MY ACCOUNT`; a client timestamp, dialog state, callback alone,
or different session is never authority. On acceptance it clears the current cookies and the UI
shows only an “underway” receipt—there is intentionally no authenticated completion poll after
tenant-wide session revocation.

Every tenant-scoped live external mutation must pass through the PostgreSQL tenant-effect authority.
It holds the same transaction-scoped advisory lock used by the erasure fence from authorization
until the already-started provider operation actually settles; after a tombstone commits, new live
effects fail closed. `EC_TENANT_EFFECT_LOCK_TIMEOUT_SECONDS` bounds lock acquisition to 0.1–30
seconds (5 by default), and `EC_TENANT_EFFECT_TIMEOUT_SECONDS` marks an effect overdue within
0.1–60 seconds (30 by default). Each HTTP/SDK adapter must have a transport deadline no greater than
the effect deadline. An overdue or caller-cancelled operation is still drained before the lock is
released, so alert on deadline breaches rather than assuming cancellation stopped an SDK thread.
The erasure worker records the post-fence drain stage before starting workflow/provider cleanup.

Before admitting traffic, require `/readyz` to report `identity=ready`. Protect Redis with network
isolation, authentication, encryption appropriate to the deployment, no-eviction capacity for the
bounded session working set, and monitoring for latency, errors, memory pressure, and key eviction.
The signed tenant/subject account mapping must be provisioned through an authenticated control-plane
process; the callback never creates an account from arbitrary IdP claims.

The consumer shell polls only while authenticated, visible, and holding a request created within six
hours that remains `received`/`started` without a selected outcome. Recent briefs use a jittered
30-second base delay with exponential failure backoff toward five minutes; Plans and To do refresh
once only when the request fingerprint changes. Navigation/manual refresh remains the path for later
lifecycle changes. Capacity estimates must include this bounded read cadence; do not broaden or
shorten it without measuring database and edge load. A transient background refresh retains the last
rendered projection, so monitoring—not a blank UI—is the signal for sustained freshness failure.
This poll is not notification delivery evidence; ADR-009's durable email ledger remains the launch
notification authority.

The lifecycle scanner spaces Temporal describe RPCs at the per-process
`EC_LIFECYCLE_INVARIANT_LIVENESS_CALLS_PER_SECOND` cadence (10 calls/second by default, validated
from 1 through 20). It still keyset-pages the full nightly PostgreSQL inventory and emits only
aggregate counts. After the first uncertain Temporal response, it stops issuing liveness RPCs for
that scan and counts every remaining nonterminal workflow as uninspectable; it never guesses that
an execution is closed or invokes a repair path. Do not scale scanner replicas or raise the cadence
without budgeting their aggregate load alongside workflow traffic and health probes.

The catalog cadence dispatcher is a bounded, one-shot scheduled job:
`python -m events_concierge.workers.catalog_refresh_dispatcher`. Source-specific refresh is also
one-shot: `python -m events_concierge.workers.catalog_refresh SOURCE_KEY`. The scheduler, approved
source registry, crawl policy, and source-legal review are production inputs; merely deploying these
commands does not authorize source egress.

The anonymous Meetup city sources use this same dispatcher and catalog policy; there is no separate
Meetup daemon. A production schedule may invoke the due-source dispatcher, but each source's
registry interval, review state, policy, approved origin, pacer, and refresh lease remain
authoritative. Follow [the Meetup ingestion runbook](meetup-ingestion-runbook.md) for source-specific
verification and containment. Never substitute the local recurring scheduler or the tenant OAuth
adapter for the reviewed production scheduling/control plane.

The hosted management profile is a separate IAP-protected frontend and operator API. It verifies
signed identity, exact-Origin JSON mutations, assigned viewer/operator/reviewer capabilities, and
server-derived receipt actors. Consumer authentication never grants operator authority. See
[ingestion administration](ingestion-admin.md#hosted-operator-boundary) for exact settings and roles.
`EC_ADMIN_INGESTION_ENABLED` remains local/mock-only.

Migration `0181` enforces stored adapter identity while preserving configuration OCC and audit;
`0182` separates controller/viewer/executor capabilities and removes consumer admin access. Drain
cadence and command execution before applying the authority split. Provision separate non-owner
LOGIN principals and versioned URL secrets, then deploy the matching API/executor images together.
Do not roll back to a consumer-credential admin image: `0182` is forward-only and does not restore
old grants. The local fixture bootstrap is never a production provisioning procedure.
The new aggregate definer requires CREATEROLE plus owner/grant authority over its eleven input
tables and the public schema; it needs no SUPERUSER/BYPASSRLS. It has SELECT-only privileges, with
an explicit policy for FORCE-RLS account erasure. On PostgreSQL 16 only trusted migrator ADMIN
metadata remains, with SET and INHERIT false. Earlier migrations still have their own privileged
owner requirements; target Cloud SQL compatibility for the full migration chain remains unproven.

With `operator.enabled`, the existing deployment CronJob invokes `ingestion_cadence --once` and
appends a deterministic durable due command. The executor owns leases and linked refresh outcomes.
The local recurring cadence loop is not production schedule authority; Temporal Schedule cutover
has not been activated. The legacy direct refresh Job is rejected by the operator profile.

Operator `/metrics` exports aggregate queue counts and available progress timestamps through a
private GMP scrape. It contains no tenant, command, or source labels. `ready` and `leased` describe
pending work; failure signals can overlap pending. Use scrape failure/age and backlog age together;
absence of a progress sample is unknown, not zero. Multiple API replicas expose the same DB totals:
use a max across replicas, never a sum. Worker liveness and Temporal poller health need independent
runtime evidence. Entity refresh remains an unleased due projection and must not be scaled based
on this snapshot.

### Entity-profile, Pacer, command-lease, and ingestion-evidence migration rollout

The current source migration head is `0182`. Migrations `0128`–`0130` are deliberately ordered but should
not be treated as a migrate-first rolling change. `0128` adds and validates checked JSONB
columns/functions for verified entity profiles, `0129` reconciles historical terminal Pacer
deferrals into retryable paused runs, and `0130` installs renewable command leases plus overview v2.
They can scan or lock ingestion tables. Schedule them in a measured maintenance window, record
table size and lock-wait telemetry, and validate on a production-shaped restore before applying.

Later additive migrations must still be applied in order. `0135` installs closed, bounded run-stage
and execution evidence; `0136` adds deterministic topic/facet and exact free-inference provenance;
`0137` advances only the two reviewed Meetup city rows to their 41-unit detail-enrichment
contract, and `0138` replaces OID-sensitive temporary topic scans with bounded record streaming.
Validate that older runs project as `legacy_unavailable`, that shared Temporal workers
leave CPU/RSS null with wall-clock-only scope, and that the Meetup row revisions/page limits match
the deployed adapter before resuming cadence.

Migrations `0139`–`0146` then tighten retired-source lifecycle, execution evidence, interval
overlap, sorting, and city-facet contracts. Migrations `0147`–`0152` add the catalog entity index,
quality gates, provider-neutral enrichment, single-scan topic facets, and retained entity history
and insights. Deploy the `0152` application image with the migration; if an older image refreshed
sources during the migration window, run the documented idempotent entity-index rebuild once after
cutover before reopening ingestion.

Drain or stop every pre-`0129` ingestion worker before running the reconciliation. Old workers can
still write the legacy terminal Pacer form after a one-time backfill, so a mixed-version worker
fleet makes the classification race unavoidable. The safe sequence is:

1. stop cadence/command dispatch and drain every old source and ingestion-command worker;
2. apply `0128`, `0129`, and `0130`;
3. verify every `paused` run has null `completed_at`, `lease_token`, and `lease_expires_at`;
4. deploy the new API, scheduler, command worker, and source workers together;
5. verify each pre-`0130` live command lease expired once, each affected command is reclaimed only
   once, and a long source run renews its command lease;
6. run the reconciliation check again before resuming cadence; and
7. alert if a new failed run contains the exact legacy `Pacer wait:`, `Pacer degrade:`, or
   `Pacer saturated:` form.

The migration intentionally recognizes only that exact bounded worker-produced form. Provider
errors that merely mention pacing remain failures. Runtime pause is lease-fenced; losing the fence
returns busy rather than claiming that the run was deferred.

Migration `0130` requires the drained ingestion command plane above. Its one-time reconciliation
expires every still-live pre-heartbeat command lease so the new worker can reclaim abandoned work
promptly; an old worker left executing is fenced from recording completion. The new worker claims a
300-second lease and renews only the exact live command/token pair every
`min(60 seconds, lease / 3)`. A false renewal result or renewal error cancels the in-flight
operation without a terminal command mutation; lease expiry and guarded reclaim choose the next
owner. Do not use downgrade to infer that prior lease timestamps were restored—the reconciliation
is intentionally irreversible.

Overview v2 reports live-running work from normalized, unexpired source-run and command-lease facts,
not stale status text. Its failed-24h counter includes only each source's latest unresolved failure,
so recovered sources and repeated attempts do not inflate the operator alarm. The local/mock
`ingestion-cadence` scheduler checks for due sources every 300 seconds; it only enqueues a durable
fleet command and never performs provider work itself.

There is no production browser-fleet worker in this repository. Do not claim the browser lane is
available until the fleet, broker, egress controls, provider adapter, and isolation evidence are
deployed.

## Data-store durability

### PostgreSQL

PostgreSQL is the lifecycle and queue system of record. Production requires a managed Multi-AZ
deployment, continuous WAL archiving/PITR, encrypted storage and backups, and an isolated restore
target. The vendor configuration must satisfy all of the following before the RPO/RTO can be claimed:

- [ ] PITR restore-point granularity and WAL archival lag are continuously measured at 60 seconds or
  less.
- [ ] Synchronous/durable commit and failover semantics are documented to prove that an acknowledged
  transaction is not lost.
- [ ] Automated full/base backups and continuous WAL retention cover the declared recovery window.
- [ ] Backup encryption keys, deletion protection, access logging, and cross-account recovery access
  survive loss of the primary account.
- [ ] The migration-owner credential is separate from the application role; the app role remains
  non-owner, non-superuser, and non-`BYPASSRLS`.
- [ ] Alerts cover replication/WAL archival lag, storage exhaustion, connection saturation,
  long-running transactions, failed backups, and restore-point age.
- [ ] A dated restore drill demonstrates actual RPO, RTO, RLS isolation, queue continuity, and serving
  recovery. A provider dashboard saying “backups enabled” is not a drill.

### Temporal

Temporal is the durable executor, not a replacement for PostgreSQL backups. Production must use TLS,
an API key from the secret manager, the intended namespace, and the same claim-check data converter
on every API and worker client. Verify namespace retention, availability, throughput, archival/export
needs, and vendor recovery behavior in the O-6 contract review.

Never purge a workflow history while a lifecycle, pending queue item, audit reference, or claim-check
object still depends on it. Monitor task-queue pollers, schedule-to-start latency, workflow failures,
non-determinism, stuck open executions, and history growth. An engine outage should grow the
start-outbox while leaving committed requests intact; recovery should drain it through
reject-duplicate starts.

Every eager connection, workflow start, signal, and execution-description read has the independently
validated `EC_TEMPORAL_RPC_TIMEOUT_SECONDS` bound (five seconds by default, 0.1–60 seconds). A start
timeout is not an acknowledgement: the request remains in `request_start_outbox` for deterministic
reject-duplicate replay after the engine or network recovers.

Production runs separate transactional and catalog roles on distinct
`EC_TEMPORAL_TRANSACTIONAL_TASK_QUEUE` and `EC_TEMPORAL_CATALOG_TASK_QUEUE` values. Each role has
explicit, validated workflow-task and activity slot limits
(`EC_TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` and
`EC_TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES`, both eight by default) and a pinned Temporal Worker
Deployment version derived from an immutable build identity. Workflow-task slots validate to 2–64
because Temporal caching requires at least two; activity slots validate to 1–64. The compatibility
combined role remains for local use, but it is not the production deployment topology. Keep
activity slots within the worker process's explicit database pool and the environment-wide Cloud
SQL budget, and size the workflow executor to the workflow-task limit. The request-start relay waits
at least
`EC_REQUEST_START_POLL_SECONDS` between all passes, including non-empty ones, and claims at most
`EC_REQUEST_START_BATCH_SIZE` parents per pass. That cadence is per relay process, so budget the
aggregate rate across replicas. Account for each parent's registration-child fanout before raising
either value. Do not mask worker saturation by increasing Temporal's workflow-task timeout:
schedule-to-start latency, database checkout timeouts, late `Task not found` completions, or SDK
deadlock warnings require backpressure or capacity correction.

Service-backed tests must never target a runtime database. The Make targets create and destroy only
randomized `ec_test_*` databases, and the integration fixture rejects any other database name. This
prevents durable test start-outbox rows from being replayed when a runtime request-start worker is
later enabled. Database/user/host/service query overrides are rejected before database creation so
the driver cannot silently route around the checked URL path.

API request bodies are capped at 64 KiB and must finish within the validated
`EC_REQUEST_BODY_TIMEOUT_SECONDS` interval (ten seconds by default, 0.1–60 seconds). Safe
body-independent routes such as health checks bypass buffering; stalled mutation bodies receive 408.

### Claim-check object storage

Claim-check objects are required to replay Temporal histories that contain opaque references. The
local filesystem implementation is not production storage. The repository's native GCS adapter uses
generation-zero conditional creation, byte-identical replay checks, bounded reads, and all-generation
tenant-prefix deletion, but those contracts still need deployment IAM, lifecycle, KMS, recovery, and
cross-tenant field proof. Production storage must be shared by all API/worker replicas and provide:

- tenant-prefixed access control, encryption at rest, TLS, immutable conditional create, integrity
  checks, versioning, and audit logs;
- backup/replication and key availability within the same RPO/RTO envelope as Temporal and
  PostgreSQL;
- lifecycle retention at least as long as every referencing workflow history, with no age-only
  deletion rule that can orphan a history;
- tested tenant-prefix erasure without cross-tenant deletion; and
- restore validation that retrieves and integrity-checks a sampled payload from a restored workflow.

### Redis

Redis holds shared pacing, fairness, and admission state; it is not lifecycle truth. Use an
authenticated, TLS, Multi-AZ service with eviction disabled for the application database and alerts
for memory pressure, failover, command latency, and unavailable scripts.

On state loss, the pacer must recover throttle-first: no cold-start burst, and browser admission must
honor its recovery fence. Temporal timers and PostgreSQL ledgers own durable work. Do not reconstruct
Redis by replaying provider calls, and do not weaken the safety fence to clear a backlog. Redis
snapshot/AOF recovery can reduce delay, but it is not evidence for the lifecycle RPO.

## Monitoring and alerting

Ship structured logs and metrics to a system independent of the application failure domain. At
minimum, dashboard and page on:

- `/healthz` and `/readyz` synthetic probes at the NFR-3 one-minute cadence, separated into database
  unready and Temporal-degraded time;
- API error/latency, database saturation, Temporal task-queue schedule-to-start latency, worker
  restarts, and absence of each required worker heartbeat;
- pending count and oldest age for `request_start_outbox`; a rising backlog plus Temporal degradation
  is expected briefly, but age beyond the incident budget pages;
- erasing account count and oldest request age, expired erasure leases, maximum attempt count,
  last-failure stage, and the account-erasure worker heartbeat; page on any stalled fenced tenant,
  because browser-request completion is not the durability boundary;
- pending/ready/leased/terminal-failed `outbox` rows, oldest ready age, notification-ledger leases,
  retry count, and provider send/delivery/bounce/suppression events;
- notification routing-to-provider-delivery latency by lane. ADR-009 requires warning/page thresholds
  at 45/90 seconds against the 120-second handoff-notification target;
- pending organizer-change deliveries and calendar repairs, overdue handoff expiry repairs, watch
  freshness, catalog cadence failures, per-source last-success age and candidate/canonical yield,
  zero-candidate shifts, source quarantines, and lifecycle-invariant findings;
- for anonymous Meetup city sources, endpoint/redirect drift, missing or malformed root Event
  JSON-LD, response-size rejection, detail identity/envelope/cap outcomes, implausible source yield
  changes, and any indication that member/RSVP/attendee material reached a shared projection;
- catalog-run stage outcomes, wall-time trends, and measurement scope/quality. Shared Temporal
  workers intentionally omit CPU/RSS; direct sequential-worker CPU and boundary RSS are best-effort
  whole-process correlations, not host utilization or a continuously sampled peak. Use independent
  host/container telemetry for saturation and capacity alerts; and
- global/tenant kill-switch state and every policy change, including actor, ticket, reason, old/new
  value, and propagation verification.

Useful read-only queue checks (run with a separately audited operations read role) include:

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

Never include notification payloads, tokens, email bodies, OAuth credentials, or claim-check bytes in
logs, metrics, traces, tickets, or chat.

The handoff-completion URL contains a one-time bearer capability. Configure CDN, load-balancer,
reverse-proxy, and APM access logs to redact the token segment on `/v1/tasks/*/done`; never emit the
full URL to telemetry. Preserve `Cache-Control: no-store` and `Referrer-Policy: no-referrer`; GET
must remain inert and only an explicit POST may signal completion. The database outbox stores only
an authenticated-encrypted projection, reveals it inside the notification relay immediately before
delivery, and scrubs the ciphertext on delivery or terminal failure.

Migration `0106` cannot safely reconstruct encryption for an already-persisted plaintext
capability. It scrubs and terminal-quarantines any such pending row (and clears its non-delivered
ledger lease); never copy the old value into a replacement. Recreate the handoff through the normal
workflow if the user still needs an action link.

## Incident controls

Use a control-plane/migration-owner session, never the ordinary application role, for operator policy
changes. Capture the incident ticket and current value before changing anything.

Freeze all autonomous mutations:

```sql
BEGIN;
SELECT public.fn_set_policy_global_kill_switch(true);
COMMIT;
```

Freeze one tenant:

```sql
BEGIN;
SELECT public.fn_set_tenant_policy_kill_switch(
    '00000000-0000-0000-0000-000000000000'::uuid,
    true
);
COMMIT;
```

The UUID above is a placeholder and must be replaced only after tenant identity is verified through
an audited operations lookup. A kill switch prevents new mutations; in-flight workflows park and
re-check. Confirm the durable control row and a pre-mutation denial before relying on it.

A trusted ban/forbidden detector may irreversibly quarantine a known source through the app role:

```sql
SELECT public.fn_quarantine_source('meetup', 'ban');
```

That function can only move `quarantined` from false to true. Operators should use it for emergency
containment too. Never rotate identities, proxies, or accounts to evade a source block. Clearing a
quarantine requires owner/legal review and the owner-only `fn_set_source_policy` path; preserve every
other source-policy field and record the review evidence.

Incident sequence:

1. Contain: engage the narrowest tenant/source control that is safe; use the global switch when blast
   radius is unknown. Disable external egress or scale the specific faulty worker to zero if policy
   propagation itself is suspect.
2. Preserve: do not delete leases, histories, outboxes, claim checks, logs, or failed rows. Snapshot
   dashboards and record provider incident identifiers.
3. Diagnose: distinguish PostgreSQL unavailability, Temporal degradation, Redis throttle-first
   recovery, notification-provider failure, source ban, and credential compromise.
4. Recover: restore dependencies first, then workers, then API traffic. Let guarded relays reclaim
   expired leases naturally; do not force acknowledgements.
5. Release controls only after a dry-run/read path and one guarded canary prove the hazard is gone.
   Tenant/global switches can be set to `false` with the same functions. Source quarantine release is
   a separate owner-reviewed policy change.
6. Verify convergence: start-outbox drained, no terminal notification failures, invariant scan clean,
   claim checks readable, and no duplicate provider/calendar effect.

## Secrets and key rotation

Production startup must fail closed unless the deployment-owned runtime provider supplies real
authentication, shared object storage, notifications, credential vault, calendar access, and
explicit registration and withdrawal source maps, plus a `NotificationSecretProtector` backed by
production KMS/envelope encryption. The built-in GCP factory currently supplies GCS, reviewed public
discovery, PostgreSQL audit/consent, explicit empty mutation maps, and optional Calendar assembly;
it intentionally leaves notifier, notification-secret protector, credential vault, and production
Calendar binding/access unprovisioned. Full preflight therefore fails. The stable local AES-GCM key
is public development scaffolding and must never protect production data. The repository includes
SDK-injected KMS-envelope and SES v2 adapters in addition to its OIDC, GCS/S3-compatible, Google
Calendar, and Meetup adapters. A real deployment must still bind and provision the KMS key/policy,
notification identity/configuration, and delivery-event/bounce/suppression pipeline. SES has no send
idempotency token, so this does not settle ADR-009's held post-send-ack ownership decision. The
repository still does not include the separately isolated production credential-vault/injection-
broker backend. Mock adapters and local claim storage are for local/test environments only.

Keep database, Redis, Temporal, OIDC, SES/inbound-email, object-storage, KMS/vault, calendar, model,
source, and browser-provider credentials in a secret manager. Use workload identity where available;
otherwise use least-privilege, separately rotatable credentials. Deny secrets in image layers,
environment dumps, workflow payloads/history, logs, traces, prompts, and tool arguments.
The committed GKE profile mounts runtime and migration secrets as files: a value and its `*_FILE`
reference are mutually exclusive, and the loader accepts only a bounded absolute regular UTF-8
file. The current shared runtime mount/IAM graph is deliberately an interim scaffold; split it by
process before least-privilege sign-off.

Standard rotation:

1. Create a second credential/key and grant the same least-privilege policy.
2. Update the secret reference and roll every consumer using one immutable release/config revision.
3. Verify authentication, claim-check read/write, Temporal polling, queue drain, and provider canary.
4. Revoke the old credential, verify failed use is visible, and record the completed rotation.

### Application database role rotation

`ec_app` is the fixed non-owner runtime role. Migration `0002` creates it as `NOLOGIN` when no
initial password is supplied, or enables login using the password read from
`EC_APP_ROLE_PASSWORD(_FILE)` by the migration-owner Job. It never contains a production default.
Once `0002` is applied, rerunning `alembic upgrade head` does not rotate the role, so use the
separate `rotate-app-role-password` operation and disabled Helm maintenance Job.

The rotation has no dual-password overlap and must use this order:

1. Schedule a maintenance interval, retain the prior password/DSN secret versions for rollback, and
   create matching new numeric versions for `EC_APP_ROLE_PASSWORD` and `EC_DATABASE_URL`. Do not
   change the running application DSN yet.
2. Quiesce API and every database-using worker. With the committed chart, render
   `global.releasePhase=role-rotation` and `jobs.roleRotation.enabled=true`; that phase removes
   application workloads and mounts only the migration-owner URL and app-role password files into
   the maintenance Job.
3. Run `python -m events_concierge.operations rotate-app-role-password`, wait for Job completion,
   and retain its sanitized report proving `role=ec_app`, login enabled, no elevated attributes,
   and no role memberships. A failed or timed-out Job means no application rollout.
4. Immediately deploy `global.releasePhase=application` with the new `EC_DATABASE_URL` numeric
   secret version, verify database readiness through `ec_app`, and complete the application canary.
5. Revoke the old secret versions only after the observation window and rollback decision expire.
   To roll back, first rerun the same gated Job with the prior password version, then redeploy the
   prior DSN and image values. Reverting Helm values alone cannot restore database authentication.

The rotation code binds the password and enables SQLAlchemy `hide_parameters`, so application-side
SQL logs do not render it. That setting covers only client logging. PostgreSQL/Cloud SQL and any
database proxy or audit sink used during migration/rotation must also disable or redact SQL
statement and bind-parameter logging; otherwise the server-side `set_config` call or an error path
can disclose the password. Never use the plaintext password in Job arguments, Helm values, shell
history, evidence, or support bundles.

For KMS envelope keys, follow the vault's rewrap/rotation procedure; do not decrypt credential
plaintext into an operator shell. The notification-secret envelope records the immutable key ID
returned by KMS, rather than the configured alias, and decrypt verifies the returned identity; alias
rotation therefore keeps old ciphertext addressable. It also uses a random per-envelope encryption-
context identifier so provider audit metadata does not contain a tenant UUID; tenant binding is in
authenticated local AAD. Preserve old key versions until every retained ciphertext and backup has a
tested recovery path. An emergency compromise rotation also revokes active OAuth/source sessions and
audits every credential access in the exposure window.

## Restore drill

Run this before launch and on a scheduled recurring basis; choose and record the production cadence
with the on-call owner. A successful drill has evidence, timestamps, and measured values.

`make restore-drill-local` is the committed CI/developer rehearsal for the PostgreSQL portion. It
requires the local API and writer workers to be stopped, takes a custom-format `pg_dump`, restores it
into a random `ec_restore_drill_*` database, compares schema head and sanitized durable aggregate
counts for the complete current table inventory—including audit, queue, policy, projection,
catalog-refresh, and account-erasure state—and every sequence position, verifies the application
role is neither superuser nor
`BYPASSRLS`, has no role-creation/database-creation/replication attributes or inherited role
memberships, requires every RLS table to use `FORCE ROW LEVEL SECURITY`, proves a two-tenant
isolation fixture through `ec_app`, and destroys the restored database in `finally`. It never starts
a worker, calls a provider, or retains the dump. Its JSON report contains no rows, DSNs, or
credentials.

That local rehearsal proves the recovery tooling contract and catches schema/grant/RLS regressions;
it does **not** prove managed PITR, WAL lag, encryption-key recovery, Temporal history replay,
claim-check recovery, actual RPO/RTO, or production network isolation. The managed drill below must
restore vendor backups into a separately fenced account/network and retain its own dated evidence.

1. Select a recovery timestamp unknown to the restore operator, record the last acknowledged test
   transaction before it, and start the RTO clock.
2. Restore PostgreSQL via PITR into an isolated network/account. Restore or attach the matching
   claim-check object-store version and ensure required KMS keys are available through recovery-only
   roles. Do not connect restored workers to real source, calendar, or notification endpoints.
3. Verify schema head, database integrity, forced RLS with the non-owner app role, audit continuity,
   lifecycle/transition counts, outbox and request-start continuity, and no plaintext secrets.
4. Connect a compatible worker build to the designated Temporal recovery/test namespace. Verify
   representative open histories replay and every sampled claim-check reference resolves with a
   valid digest.
5. Start relays with external effects replaced by audited test endpoints. Prove reject-duplicate
   request starts, notification-ledger dedup, calendar/provider idempotency, and lease fencing.
6. Run the lifecycle invariant scanner, authenticated API canary, and `/readyz`; record when the
   restored serving surface becomes ready and stop the RTO clock.
7. Calculate the actual gap between the recovery point and the last acknowledged test transaction.
   The drill passes only if it is at most 60 seconds, no acknowledged transaction is missing, serving
   recovery is at most 24 hours, and projections are rebuildable.
8. Destroy isolated restored plaintext/ciphertext according to retention policy, retain non-secret
   drill evidence, and open tracked actions for every gap. Do not mark NFR-13 achieved until a rerun
   closes them.

The drill must also simulate a Temporal outage: accept durable intake, observe pending
`request_start_outbox`, restore the engine, and prove exactly one parent workflow per request while
in-flight state resumes without a duplicate external effect.

## External launch gates

The following require credentials, contracts, domains, production infrastructure, legal/owner
judgment, or field evidence and cannot be closed by the offline test suite:

- [ ] Attach the recovered local repository to the intended private upstream, reconcile it with any
  authoritative prior history, then enable protected review, secret scanning, and required CI
  checks. Do not force-push this new root history until the upstream and reconciliation strategy are
  confirmed.
- [ ] Ratify or supersede every unchecked item in
  [the owner decision brief](../design/owner-decisions.md), including the Temporal outage posture and
  notification-channel riders.
- [ ] Resolve P20 calendar upsert/transition recovery semantics and the ADR-009 post-send
  acknowledgement ownership decision before changing either recovery contract. P20 must also
  settle the deferred `organizer_change_applied=False` tri-state contract.
- [ ] Assign an authenticated operator owner, resolution SLA, and guarded approve/reject command for
  `handoff_completion_attempts.outcome = 'review_required'`. The foundation records the discrepancy,
  blocks the calendar write, and notifies the user, but deliberately exposes no unauthenticated or
  ad hoc database path that can override the receipt.
- [ ] Obtain owner sign-off on draft requirements v0.3 and either implement and verify FR-11–FR-18
  or record an explicit signed launch deferral; D9 and D10 remain held.
- [ ] Approve the retention, legal-hold, pseudonymous tombstone/session-fence, and retained-audit
  policy. The repository now provides a fail-closed resumable erasure coordinator plus one shared
  PostgreSQL tenant-effect authority for every enabled RSVP/provider, Calendar, claim-check/object,
  notification, withdrawal, and Temporal-start path. Cancellation-safe unit and exact database race
  tests prove that an admitted effect drains before the erasure lock is released and that later
  effects are refused. FR-10.5 remains open until every real deployment adapter is field-proved with
  isolated cross-tenant fixtures, retention and backup copies are covered, and the currently
  disabled Google watch-creation path gains a durable create/store/stop-channel erasure protocol
  before activation.
- [ ] Prove Temporal Cloud's asynchronous execution deletion physically completes within NFR-11's
  72-hour window. `DescribeWorkflowExecution -> NOT_FOUND` proves the execution is no longer
  addressable, not that history-store deletion has physically completed. Disable archival/history
  export or separately purge and verify every copy, including late-start histories.
- [ ] Prove Calendar cleanup from an immutable begin-time inventory. A previously provisioned but
  now missing binding/access token must fail closed rather than report success; large calendars must
  converge through bounded resumable pagination; pre-marker events need a backfill/dedicated-calendar
  purge strategy; and 404, foreign-event, token-cycle, cap, cancellation, and retry cases need field
  evidence.
- [ ] Complete O-6 Temporal Cloud contract checks: SLA at least 99.9%, namespace capacity around
  1,600 peak transitions/second, payload/retention/archival terms, recovery behavior, and cost basis.
- [ ] Provision production PostgreSQL/PITR, Redis, GCS claim-check storage, KMS/vault, secret
  manager, and OIDC issuer/audience/JWKS. Complete the built-in GCP provider's missing notifier,
  notification-secret protector, credential vault, and production Calendar binding/access; require
  full non-mock preflight to pass, then pass the managed restore drill.
- [ ] Register and secret-provision a real confidential OIDC client for the built-in BFF, provision
  the signed tenant/subject account mapping, and verify the real issuer/audience/JWKS, callback, TLS
  edge, Redis isolation/capacity,
  login/purpose-bound reauthentication/logout/expiry/tenant-wide-revocation behavior, and absence of
  mock onboarding/local tenant-header fallback.
- [ ] Run a deployment-level browser canary against the real production-shaped BFF and CSRF policy,
  including login/reauthentication/logout/expiry and edge-header behavior. The committed
  Playwright/CI suite already covers deterministic product flows, selected-outcome rendering, exact
  typed erasure confirmation, accepted non-polling state and browser-session teardown, desktop and
  390 px layouts, keyboard/focus and status/error behavior, reduced motion, security headers,
  horizontal overflow, and console errors; it does not establish real-IdP purpose-bound
  reauthentication, production identity safety, or complete WCAG conformance.
- [ ] Complete G1 with a realistic natural-language request corpus and use the measured lane mix to
  resize browser capacity, Ticketmaster reserve, and handoff staffing.
- [ ] Complete G2 with a Meetup Pro OAuth consumer and test account before enabling autonomous Meetup
  or declaring its SLA/quota assumptions.
- [ ] Complete G3 using the real relay domain and inbound routing before enabling OTP/magic-link
  account linking.
- [ ] Finish Google OAuth Production publishing and sensitive-scope verification; provision
  tenant-scoped token lifecycle and calendar bindings. Permanently forbid Gmail scopes.
- [ ] Provision distinct inbound relay and outbound notification domains, SES identity/warm-up,
  delivery-event ingestion, bounce/suppression handling, address verification, and pager routing.
- [ ] Complete Browserbase/fleet isolation, ZDR, egress, concurrency, injection-broker, and
  credential-transit reviews before enabling browser registration.
- [ ] Complete source-specific ToS/commercial-use/legal review and owner-controlled activation.
  Disabled or quarantined sources stay disabled; deployment is not activation.
- [ ] Deploy independent metrics/logging/tracing, synthetic canaries, alert routes, worker-heartbeat
  monitors, a staffed on-call rotation, and rehearse kill-switch/quarantine/channel-swap incidents.

Related design authority:
[ADR-004](../decisions/adr-004-data-plane-policy-killswitch.md),
[ADR-007](../decisions/adr-007-db-anchored-lifecycle.md),
[ADR-009](../decisions/adr-009-email-launch-notification-channel.md),
[ADR-010](../decisions/adr-010-temporal-cloud-engine.md),
[ADR-011](../decisions/adr-011-relay-inbox-no-gmail.md), and
[ADR-012](../decisions/adr-012-same-origin-static-consumer.md).
