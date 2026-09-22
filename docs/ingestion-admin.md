# Ingestion administration

Inspect catalog collection, investigate failures and submit reviewed commands. Use an existing authorized runtime; deployment procedures belong to the [private runbook](../deploy/development.md).

## Access and controls

- Local Next.js: `http://127.0.0.1:3001/admin`; FastAPI fallback: `http://127.0.0.1:8000/admin`.
- Local admin requires mock mode, loopback bind and loopback `Host`; never publish its API port on `0.0.0.0`.
- Consumer authentication grants no operator authority.
- Admin uses `EC_OPERATOR_DATABASE_URL`; collection uses `EC_INGESTION_EXECUTOR_DATABASE_URL`. Neither inherits consumer/owner privileges.
- Local migrations bootstrap `ec_local_operator` and `ec_local_ingestion`; changed passwords require matching URL overrides.

| Role | Controls |
| --- | --- |
| Viewer | Read safe catalog, source, run, command and background-work projections. |
| Operator | Queue refresh; pause/resume reviewed sources using exact revisions. |
| Reviewer | Edit reviewed source configuration with explicit acknowledgement and revision checks. |

- Bulk pause/resume is atomic, audited and limited to 100 sources; incomplete roster reads disable bulk changes.
- Reviewer fields: seed URL, approved HTTPS origins, enabled state, cadence, pacing, page limit, collection horizon and review expiry.
- Source creation/deletion, identity, adapter mode, handoff authority, quarantine release and fleet policy require separately reviewed control-plane work.
- Enabling a source cannot bypass review freshness, policy, approved origins or the adapter's closed fetch contract.
- [Admin API](../src/events_concierge/api/admin.py) and [operator API](../src/events_concierge/api/operator.py) own authorization and mutation contracts.

## Hosted operator boundary

```bash
uvicorn events_concierge.api.operator:create_operator_app --factory
```

- Requires `EC_OPERATOR_API_ENABLED=true`, non-mock composition, exact HTTPS `EC_OPERATOR_PUBLIC_ORIGIN`, `EC_OPERATOR_IAP_AUDIENCE` and `EC_OPERATOR_SUBJECT_ROLES`.
- The API verifies Google's signed IAP assertion; unsigned headers and consumer cookies confer no authority.
- Mutations require JSON and the exact browser Origin. The verified subject supplies the receipt actor.
- The pool validates `ec_operator_controller` membership and rejects consumer/elevated logins; executor credentials have separate command/publication capabilities.
- Restricted definer functions expose bounded aggregates and opaque references, never raw queue tables, tenant payloads or lease tokens.
- Entity refresh remains unleased; overview counts do not establish safe scaling or worker liveness.
- [Helm operator profile](../deploy/helm/events-concierge/README.md#hosted-operator-profile) owns IAP, identities, mounts and deployment prerequisites. Rendering does not establish activation.

## Execution model

```text
Cadence/operator → durable command → ingestion-command worker
  → registry/review/policy → Redis pacing → source lease
  → bounded collection → normalize/dedupe → fenced publication
```

- HTTP acceptance persists work only; it never contacts event providers.
- Local cadence probes due work every 300 seconds by default and appends a deterministic slot command.
- Hosted cadence uses `ingestion_cadence --once` from the chart CronJob; a production recurring daemon is rejected.
- Temporal Schedule cutover remains a separate deployment decision.
- Each command freezes its source plan; one source executes per claim. Continuations release the lease and preserve completed tasks.
- Database-clock eligibility lets waiting manual commands run between fleet sources. Deferred tasks retain their run identity.
- Retry budgets are per source task: five for exact-source work, fifty for due-fleet work.
- Command leases default to 300 seconds, renewed every `min(60 seconds, lease / 3)`.
- Failed renewal cancels work and forbids terminal command writes. Expiry and guarded reclaim select the next owner.
- Source publication has its own lease fence; failed refreshes preserve the last successful catalog.
- Catalog Temporal workers use the executor role, catalog queue and catalog payload namespace; transactional workers cannot substitute.
- Drain old catalog workflows before queue/converter changes. [Migration sequence](production-operations.md#entity-profile-pacer-and-command-lease-migration-rollout).

**Collection windows**

- Each run freezes a database-timed event interval and source revision; retries retain that interval.
- Attempt evidence records admitted settings separately from current registry settings. Missing older evidence stays unknown.
- The configurable 1–90-day horizon limits collection, not retention; omission affects membership only inside a successfully covered window.
- Expired windows or old staged pages without captured bounds require a fresh run; never invent replacement bounds.
- Historical catalog records remain retained; repeated publication counts are not new/changed/unchanged event counts.
- [Command execution](../src/events_concierge/application/ingestion_command_execution.py), [refresh admission](../src/events_concierge/application/catalog_refresh.py) and [publication](../src/events_concierge/adapters/postgres/catalog_refresh_commit.py) own these guarantees.

## Investigate work

1. Inspect source admission, last success, next eligibility and current configuration in **Sources**.
2. **Queue refresh** submits work; **Refresh source details** only reloads evidence.
3. Follow **Track command** through claims, source tasks and linked runs.
4. Confirm a successful source run and published records; dispatch completion alone is insufficient.
5. Compare consumer results under matching filters; operator retained inventory and consumer eligibility differ.

| Evidence | Meaning |
| --- | --- |
| `queued` command | Saved; awaiting an eligible worker claim. |
| `running` command | Claimed; inspect lease freshness. |
| `completed` command | Dispatch finished; a queued Temporal result still awaits publication. |
| `failed` command | Bounded dispatch failure; inspect the source ledger separately. |
| `deferred` / `busy` source task | Waiting for guarded retry while attempts remain. |
| `retry_exhausted` source task | Per-source attempt budget reached; inspect the retained outcome. |
| `lease_lost` run | Expired authority; not live work. |
| Due source | Last successful completion plus configured interval has elapsed; admission may still block it. |
| Missing/failed read | Unknown or unavailable, never an inferred zero. |

- Source, command and run links preserve investigations. Example: `/admin?tab=sources&source_selection=luma-sf`.
- Run search/filtering precedes server pagination; anchored start intervals are inclusive/exclusive and bounded to 90 days.
- **Follow active run** polls every five seconds while visible; stops at terminal state or read failure.
- Command progress retains bounded structured events, not raw logs; stage observations and lease heartbeats are different evidence.
- Historical fixtures are hidden by default, not deleted. Tests use disposable databases; never reset retained data to clean a dashboard.
- Retained catalog records may include past, paused-source or cancelled entries. Later refreshes can replace source/run attribution.
- A run's currently linked records need not equal its original published count; no inventory/version history is reconstructed.

**Build and resource evidence**

- API acceptance, worker claim and executing activity identities are separate. `claim_recorded` does not prove that worker fetched the provider.
- `legacy_unavailable` stays unattributed; dirty revisions or missing digests cannot establish an immutable published build.
- Admission and collection have timers; collection includes extraction/enrichment. Publication timing includes normalization/deduplication.
- Shared Temporal activities report wall time only. Direct-worker CPU/RSS samples describe process boundaries, not host utilization or continuous peaks.
- Use container/host telemetry for capacity. Provider bodies, secrets, arbitrary exceptions and tenant data stay outside admin projections.

## Diagnostics and commands

- Widespread `503`: check `/readyz`, database readiness and storage before retrying individual views.
- Docker disk exhaustion can occur with free macOS space. Preserve volumes, running containers and release/rollback images.
- Reclaim only verified unreferenced build cache; never reset the database as a storage repair.

```bash
docker compose exec -T postgres df -h /var/lib/postgresql/data
docker system df
make app-logs
```

The following local targets start dependencies and run migrations. Use only in an authorized local environment; refresh/dispatch can contact providers.

```bash
make ingestion-commands
make ingestion-cadence
make catalog-workflow-worker
make catalog-refresh SOURCE_KEY=luma-sf
make catalog-cadence
```

- Outside Compose, recurring cadence requires `EC_CATALOG_INGESTION_SCHEDULER_ENABLED=true`; interval range is 60–3,600 seconds.
- Commands retain registry, policy, pacing and source-lease guards. [Make targets](../Makefile).
- UI behavior checks use intercepted read-only fixtures against an existing Next.js frontend:

```bash
EC_ADMIN_WEB_URL=http://127.0.0.1:3001 uv run python -m pytest \
  -o addopts='-ra --import-mode=importlib' -q tests/e2e/test_admin_operations.py
```

## Local source-policy opt-in

- Public browser collection starts fail-closed. Enabling it admits every individually enabled, reviewed registry source; inspect that list first.
- These examples replace the **local seed policy**: paid disabled, unquarantined, no signed-agent mode. Do not overwrite existing incident controls.
- Read `source_policy` first; a quarantine requires its separate reviewed release, not this opt-in.

```bash
docker exec events-concierge-postgres-1 \
  psql -U ec -d ec -v ON_ERROR_STOP=1 \
  -c "SELECT public.fn_set_source_policy(
        'public_jsonld', '{\"browser\": true}'::jsonb, false, false, 'none'
      );"
```

Restore that local seed's fail-closed posture:

```bash
docker exec events-concierge-postgres-1 \
  psql -U ec -d ec -v ON_ERROR_STOP=1 \
  -c "SELECT public.fn_set_source_policy(
        'public_jsonld', '{}'::jsonb, false, false, 'none'
      );"
```

Production policy changes require owner/legal review and the [audited incident controls](production-operations.md#incident-controls). Use the [Meetup runbook](meetup-ingestion-runbook.md) for its narrower source contract.
