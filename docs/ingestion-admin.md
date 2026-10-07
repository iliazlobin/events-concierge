# Ingestion administration

Inspect catalog collection, investigate failures and submit reviewed commands. Use an existing authorized runtime; deployment procedures belong to the [private runbook](operations/README.md).

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
| Reviewer | Edit reviewed source configuration and model budgets with revision checks. |

- Bulk pause/resume is atomic, audited and limited to 100 sources; incomplete roster reads disable bulk changes.
- Reviewer fields: seed URL, approved HTTPS origins, enabled state, cadence, pacing, page limit, collection horizon and review expiry.
- Source creation/deletion, identity, adapter mode, handoff authority, quarantine release and fleet policy require separately reviewed control-plane work.
- Enabling a source cannot bypass review freshness, policy, approved origins or the adapter's closed fetch contract.
- [Admin API](../src/events_concierge/api/admin.py) and [operator API](../src/events_concierge/api/operator.py) own authorization and mutation contracts.

## Hosted operator boundary

```bash
uvicorn events_concierge.api.operator:create_operator_app --factory
```

- Requires `EC_OPERATOR_API_ENABLED=true`, non-mock composition, exact HTTPS `EC_OPERATOR_PUBLIC_ORIGIN`, `EC_OPERATOR_AUTH_PROVIDER=iap`, the actual backend's `EC_OPERATOR_IAP_AUDIENCE` and a pinned `EC_OPERATOR_POLICY_VERSION`.
- The API verifies Google's signed assertion, backend audience and private [RBAC binding](operations/operator-access.md); unsigned headers and consumer cookies confer no authority. [Admin routing and acceptance](operations/public-access.md#admin).
- Mutations require JSON and the exact browser Origin. The verified subject supplies the receipt actor.
- The pool validates `ec_operator_controller` membership and rejects consumer/elevated logins; executor credentials have separate command/publication capabilities.
- Restricted definer functions expose bounded aggregates and opaque references, never raw queue tables, tenant payloads or lease tokens.
- Full-profile external entity refresh remains unleased; discovery exposes reads only. Overview counts do not establish safe scaling or worker liveness.
- [Helm operator profile](../deploy/helm/events-concierge/README.md#hosted-operator-profile) owns signed identity, mounts and deployment prerequisites. Rendering does not establish activation.

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
- A successful manual refresh newer than a parked cadence failure restores the next scheduled slot. Failed manual retries leave the cadence backoff and attempt history intact.
- Catalog Temporal workers use the executor role, catalog queue and catalog payload namespace; transactional workers cannot substitute.
- Drain old catalog workflows before queue/converter changes. [Migration sequence](operations/release.md#entity-profile-pacer-and-command-lease-migration-rollout).

**Collection windows**

- Each run freezes a database-timed event interval and source revision; retries retain that interval.
- Attempt evidence records admitted settings separately from current registry settings. Missing older evidence stays unknown.
- The configurable 1–90-day horizon limits collection, not retention; omission affects membership only inside a successfully covered window.
- Expired windows or old staged pages without captured bounds require a fresh run; never invent replacement bounds.
- Historical catalog records remain retained; repeated publication counts are not new/changed/unchanged event counts.
- [Command execution](../src/events_concierge/application/ingestion_command_execution.py), [refresh admission](../src/events_concierge/application/catalog_refresh.py) and [publication](../src/events_concierge/adapters/postgres/catalog_refresh_commit.py) own these guarantees.

**University feeds**

- LiveWhale requests explicit collection dates and reads every declared page within the reviewed source cap. Its [public API](https://support.livewhale.com/live/blurbs/json-api) supplies pagination metadata.
- Transport errors, invalid pages, changing totals, repeated pages and incomplete pagination fail the run; the previous publication stays available. A valid empty feed is a successful zero result.
- A cap error reports the required pages. Review the source's page budget before increasing it; never treat a truncated feed as complete. [Adapter contract](../src/events_concierge/adapters/livewhale/source.py).

**City calendars**

- Campbell's city-wide RSS profile uses `CID=All-calendar.xml&ModID=58`, the `/m/calendar` namespace and path-based event links. It retains the single-request, 50-item and 1 MB caps. Changing the registry seed requires reviewer acknowledgement.
- The older recreation-category profile remains supported while registry consumers still use it; remove it after reviewed seed migration. [Closed RSS profiles](../src/events_concierge/adapters/civic_engage/source.py).

**Library feeds**

- Berkeley's [Communico collector](../src/events_concierge/adapters/communico/source.py) reads one approved public endpoint over the configured horizon, stopping streamed responses above 3 MB. Arrays at the 600-item ceiling fail instead of publishing an incomplete calendar.
- Transport and HTTP 5xx failures use the existing bounded retry budget. Invalid feeds, redirects and other HTTP errors fail while retaining the last publication.

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

## Models

- Open `/admin?tab=models` for OpenRouter spend, tokens, latency and outcomes. Choose 24 hours, 7/30/90 days, custom UTC dates or an actual model; bookmarks retain these filters.
- Line charts show interval totals, input/output tokens and failed calls. Hover, focus or use arrow keys for interval details; missing latency and pre-accounting history stay gaps.
- Application history starts when [model accounting](../src/events_concierge/adapters/agent_runtime/openrouter/runtime.py) is activated. Chat and the development CLI record each physical request, including tool-loop calls and fallback models. Missing costs remain unknown; totals include only provider-reported USD. No prompts, answers or keys are retained.
- Reviewer budget edits are audited and revision checked. Daily/monthly USD limits start unset; zero stops new calls in enforcement mode. Warning mode only shows alerts.
- Limits use all application calls, independent of chart filters, and reset at midnight UTC. Enforcement stops new calls after recorded spend reaches a limit or a completed/stale pending charge is unknown in the active budget period. Calls already admitted can exceed the limit.
- OpenRouter key totals are separate and may include other applications. The operator service needs its own approved `EC_OPENROUTER_API_KEY` configuration for the read-only [current-key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key); management keys and account-credit access are unnecessary.
- Before activation, migrate through `0201` and release compatible API/web artifacts using the deployment runbook. Rollback retains ledger/audit tables; downgrade refuses to discard recorded history. Database unavailability denies model egress.
- Unknown charges: review [OpenRouter activity](https://openrouter.ai/activity) before changing the budget mode or waiting for the affected UTC period to reset. Historical reconciliation is not automated.

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

Production policy changes require owner/legal review and the [audited incident controls](operations/maintenance.md#incident-controls). Use the [Meetup runbook](meetup-ingestion-runbook.md) for its narrower source contract.

## Public source coverage

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
