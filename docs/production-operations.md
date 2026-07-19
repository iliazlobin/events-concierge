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

## Release and deployment order

Use one immutable image digest for the migration job, API, and all workers. Production configuration
and secrets come from the deployment platform; never copy a populated `.env` into an image.

1. Confirm an on-call owner, change ticket, rollback image digest, current database migration head,
   and a fresh recoverable database restore point. Record global, tenant, and source control state
   plus the current queue baselines. If any kill switch or quarantine is engaged, preserve it; only
   the incident owner may authorize release through an audited change.
2. Build and scan the locked image. Run lint, type checks, unit tests, migration tests, integration
   tests, and Temporal replay/compatibility tests against that exact source revision.
3. Run `alembic heads` and require exactly one head. Apply `alembic upgrade head` once, as the
   migration-owner role. Application processes must use the non-owner, non-`BYPASSRLS` role.
4. Start or roll the Temporal workflow/activity worker and durable relay/repair workers listed
   below. Verify task-queue polling, database connectivity, and worker log heartbeats before
   admitting new traffic.
5. Start or roll the API. Keep a new replica out of service until `/readyz` succeeds.
6. Send a synthetic authenticated request, verify one deterministic request row and start-outbox
   result, then verify the workflow and notification ledgers converge without duplicate effects.
7. Compare queue age, error rate, latency, and lifecycle-invariant findings with the pre-release
   baseline. Complete the change only after the observation window is clean.

Catalog refresh commands are separately scheduled, policy-gated jobs. A release must not implicitly
enable a source, run a live crawl, or turn a one-shot dispatcher into an unbounded loop.

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

## Health, readiness, and engine degradation

`GET /healthz` is shallow process liveness. It returns success while the process event loop can serve
HTTP and must not be used to assert dependency health.

`GET /readyz` is traffic readiness:

- PostgreSQL unavailable: returns `503`; remove the replica from service. Intake cannot be durably
  committed without PostgreSQL.
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

## Required process inventory

Run each long-lived process independently so one backlog or crash does not stop another. Scale only
processes whose lease/idempotency contract permits concurrency.

| Process | Command | Required responsibility |
|---|---|---|
| API | `uvicorn events_concierge.api.app:app --no-access-log` | Durable intake, authenticated feed/actions, liveness/readiness; path logging stays disabled because completion URLs carry capabilities |
| Temporal workflow/activity worker | `python -m events_concierge.workflows.worker` | Parent/child workflows and registered activities on `EC_TEMPORAL_TASK_QUEUE` |
| Request-start relay | `python -m events_concierge.workers.request_starter` | Replays `request_start_outbox` after API or Temporal failure |
| Notification relay | `python -m events_concierge.workers.notifier` | Delivers transactional outbox rows through the notification ledger |
| Change-delivery worker | `python -m events_concierge.workers.change_detection` | Signals organizer changes and drains associated repair work |
| Handoff-expiry repair | `python -m events_concierge.workers.handoff_expiry` | Repairs orphaned handoff TTL transitions after the Temporal grace period |
| Lifecycle invariant scanner | `python -m events_concierge.workers.lifecycle_invariants` | Read-only lifecycle/watch/handoff/Temporal divergence scan |

The catalog cadence dispatcher is a bounded, one-shot scheduled job:
`python -m events_concierge.workers.catalog_refresh_dispatcher`. Source-specific refresh is also
one-shot: `python -m events_concierge.workers.catalog_refresh SOURCE_KEY`. The scheduler, approved
source registry, crawl policy, and source-legal review are production inputs; merely deploying these
commands does not authorize source egress.

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

API request bodies are capped at 64 KiB and must finish within the validated
`EC_REQUEST_BODY_TIMEOUT_SECONDS` interval (ten seconds by default, 0.1–60 seconds). Safe
body-independent routes such as health checks bypass buffering; stalled mutation bodies receive 408.

### Claim-check object storage

Claim-check objects are required to replay Temporal histories that contain opaque references. The
local filesystem implementation is not production storage. Production storage must be shared by all
API/worker replicas and provide:

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
- pending/ready/leased/terminal-failed `outbox` rows, oldest ready age, notification-ledger leases,
  retry count, and provider send/delivery/bounce/suppression events;
- notification routing-to-provider-delivery latency by lane. ADR-009 requires warning/page thresholds
  at 45/90 seconds against the 120-second handoff-notification target;
- pending organizer-change deliveries and calendar repairs, overdue handoff expiry repairs, watch
  freshness, catalog cadence failures, source quarantines, and lifecycle-invariant findings; and
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
production KMS/envelope encryption. The stable local AES-GCM key is public development scaffolding
and must never protect production data. The concrete OIDC and S3-compatible adapters
still require deployment-owned issuer/client and storage-client/bucket construction; this repository
does not include a production notifier or credential-vault backend. Mock adapters and local claim
storage are for local/test environments only.

Keep database, Redis, Temporal, OIDC, SES/inbound-email, object-storage, KMS/vault, calendar, model,
source, and browser-provider credentials in a secret manager. Use workload identity where available;
otherwise use least-privilege, separately rotatable credentials. Deny secrets in image layers,
environment dumps, workflow payloads/history, logs, traces, prompts, and tool arguments.

Standard rotation:

1. Create a second credential/key and grant the same least-privilege policy.
2. Update the secret reference and roll every consumer using one immutable release/config revision.
3. Verify authentication, claim-check read/write, Temporal polling, queue drain, and provider canary.
4. Revoke the old credential, verify failed use is visible, and record the completed rotation.

For KMS envelope keys, follow the vault's rewrap/rotation procedure; do not decrypt credential
plaintext into an operator shell. Preserve old key versions until every retained ciphertext and
backup has a tested recovery path. An emergency compromise rotation also revokes active OAuth/source
sessions and audits every credential access in the exposure window.

## Restore drill

Run this before launch and on a scheduled recurring basis; choose and record the production cadence
with the on-call owner. A successful drill has evidence, timestamps, and measured values.

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
- [ ] Approve the retention, legal-hold, tombstone, and active-workflow fencing policy, then implement
  and test full account erasure across PostgreSQL lifecycle/behavioral data, RelayInbox and
  OAuth/provider state, calendar entries, audit PII, and claim checks. The existing tenant-prefix
  object purge is not full FR-10.5 erasure.
- [ ] Complete O-6 Temporal Cloud contract checks: SLA at least 99.9%, namespace capacity around
  1,600 peak transitions/second, payload/retention/archival terms, recovery behavior, and cost basis.
- [ ] Provision production PostgreSQL/PITR, Redis, claim-check storage, KMS/vault, secret manager,
  OIDC issuer/audience/JWKS, and the deployment-owned runtime provider factory; pass the restore drill.
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
[ADR-010](../decisions/adr-010-temporal-cloud-engine.md), and
[ADR-011](../decisions/adr-011-relay-inbox-no-gmail.md).
