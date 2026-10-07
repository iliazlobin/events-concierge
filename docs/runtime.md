# Runtime

Process roles, queues, durability and health signals. [Operations](operations/README.md) owns procedures; [Helm](../deploy/helm/events-concierge/README.md) owns configuration. Select processes from the installed profile, never from an unverified source example.

## Required process inventory

[Helm values](../deploy/helm/events-concierge/values.yaml) select processes. Independent long-lived workers; bounded one-shot CronJobs use `concurrencyPolicy: Forbid`. Scale only within lease/idempotency and database budgets.

| Discovery process | Entry point / boundary |
| --- | --- |
| Next.js | One `node server.js`; consumer `EC_API_ORIGIN`, protected admin `EC_OPERATOR_API_ORIGIN`; no backend credentials |
| Consumer API | `uvicorn events_concierge.api.app:app --no-access-log`; ClusterIP-only |
| Catalog cadence | `python -m events_concierge.workers.ingestion_cadence --once`; append deterministic commands |
| Ingestion-command worker | `python -m events_concierge.workers.ingestion_commands`; claim/lease commands |
| Catalog Temporal worker | `EC_TEMPORAL_WORKER_ROLE=catalog python -m events_concierge.workflows.worker` |
| Account-erasure worker | `python -m events_concierge.workers.account_erasure`; fenced cleanup and session revocation |
| Operator API | Separate FastAPI process and restricted controller DB login; verifies IAP signature/audience and [configured RBAC](operations/operator-access.md). Shared Next.js `/admin` enters through its IAP backend Service; consumer identity grants no operator access |

- Proxy strips untrusted forwarding/hop headers; 64 KiB request cap and bounded body deadline. Secure cookies, exact-Origin CSRF and safe redirects remain required.
- Public `8080` and IAP `8082` share a credential-free web Pod; Next.js `3000` is not reachable from GFE. Public assets contain no private data. Consumer/IAP sessions are separate; same-origin XSS can act as a signed-in owner. [Route and activation gates](operations/public-access.md).
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

## Durability and health

| Component | Operational constraint |
| --- | --- |
| PostgreSQL | Lifecycle/queue truth; runtime roles non-owner/non-superuser/non-BYPASSRLS; separate migration owner; protected encrypted backups and recovery keys |
| Payload storage | Shared tenant-prefixed storage; TLS/encryption, immutable conditional writes, integrity/versioning/audits; retention ≥ referencing histories; no age-only orphan deletion |
| Redis | Sessions/pacing/admission, not lifecycle truth; authenticated verified TLS/no eviction; restore throttle-first without weakening admission fences or replaying provider effects |
| Transport | Direct PostgreSQL `sslmode=verify-full`; or Cloud SQL Proxy at `127.0.0.1`, explicit port, `sslmode=disable` only for the Pod-local hop; verified `rediss`; explicit GCS project/bucket/prefix |
| Current recovery | [Private backup/restore](operations/recovery.md#manual-recovery); Redis excluded from database/payload backups; preserve sessions/fences/backoffs separately |

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
