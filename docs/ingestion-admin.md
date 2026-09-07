# Ingestion administration

The ingestion control room is a local operational surface for inspecting the reviewed public-event
catalog and submitting durable refresh commands. Start the complete stack, then open:

```text
http://127.0.0.1:3001/admin
```

The FastAPI-packaged fallback is available at `http://127.0.0.1:8000/admin`.
The consumer app and admin app are intentionally separate. Consumer tenant authentication never
grants operator authority, and the admin routes are not installed outside explicit local/mock mode.
A loopback `Host` is required on every admin request, and startup also rejects
`EC_ADMIN_INGESTION_ENABLED=true` unless the configured `EC_API_BIND_ADDRESS` is a loopback address.
Do not publish the Compose API port on `0.0.0.0` while using this local control room.
A production deployment still needs a distinct operator identity/session and preferably a separate
database role before this surface can be enabled.

## Operator workspaces

The admin is a compact, URL-addressable operations console rather than one long source table:

- **Overview** is the compact operator workspace: current fleet posture, the small set of sources
  that need action, and a short operating trend. It links to the owning workspace instead of
  duplicating detailed run, source, or command evidence.
- **Pipeline** is the aggregate five-stage fleet view. It combines admission posture, candidate and
  canonical throughput, current catalog size, success, yield, latency, and interval drill-downs.
  It intentionally does not repeat the per-run ledger.
- **Sources** filters the reviewed registry by text, collection mode, publisher, region, admission
  state, and an optional page-local health view. Operators can select one page or all matching rows
  and apply an audited pause/resume change to at most 100 sources at once. Selecting a source opens
  its end-to-end workspace.
- **Runs** is the dedicated execution-evidence workspace. It filters durable refresh history by
  exact source key, run state, and a server-side 24-hour, 7-day, or 30-day window; offers current
  source state or bounded attempt-history lenses; and expands one row at a time into typed,
  chronological execution evidence.
- **Commands** follows accepted operator work separately from source execution and supports
  status, action, source, and command-ID investigation.

Tabs and a selected source are encoded in the URL so an investigation can be shared or reopened.
For example, the Luma Bay Area Discover source can be opened directly at:

```text
http://127.0.0.1:3001/admin?tab=sources&source=luma-sf
```

The Overview launcher searches display name, publisher, source key, region, collection mode, and
seed URL, so entering `luma` finds **Luma Bay Area** and **Luma New York**. Selecting either opens
the same bounded source workspace. In secondary diagnostics these sources belong to the
`luma_discover_json` collection-adapter family; adapter labels are implementation groupings, not
source names.

The source workspace keeps only source-owned information without exposing raw provider material:

1. A compact five-stage latest-run strip: admission → collect → extract/enrich →
   normalize/dedupe → catalog publish. **View in Pipeline** opens the aggregate workspace already
   filtered to this source.
2. A concise latest-run summary with a **View all runs** handoff to the dedicated Runs workspace.
3. Searchable, paged current catalog evidence for inspecting the events the source actually
   projects.
4. Collapsed reviewed configuration and revision identity for deliberate source changes and build
   correlation. The editor can change the seed URL, approved origins, enabled state, cadence,
   pacing, page bound, and optional review expiry without changing adapter identity or authority.

The public JSON-LD ownership map currently points operators to:

- `src/events_concierge/adapters/crawl/source.py` for HTTPS collection and JSON-LD extraction.
- `src/events_concierge/adapters/meetup_city/source.py` for the anonymous, exact-profile Meetup
  city-page JSON-LD contract.
- `src/events_concierge/application/catalog_refresh.py` for policy, pacing, leases, and refresh
  orchestration.
- `src/events_concierge/adapters/postgres/catalog_refresh_commit.py` for normalization, dedupe,
  and atomic persistence.

## Execution model

```text
Admin browser
    │  POST command (202 Accepted)
    ▼
PostgreSQL ingestion-command queue
    │  lease/reclaim + database-clock retry
    ▼
ingestion-commands worker
    │
    ├─ refresh_source ─► mode-aware catalog refresh router
    └─ refresh_due    ─► bounded cadence dispatcher
                              │
                              ▼
      registry/review → source policy → Redis pacer → refresh lease
                              │
                              ▼
      collect → extract/enrich → normalize/dedupe → atomic catalog publish
```

The local/mock app profile also runs a separate `ingestion-cadence` scheduler. It reads at most one
row from the reviewed due-source projection every 300 seconds and appends a fleet
`refresh_due` command only when that probe finds work. It never calls a provider itself. One
deterministic UUID is derived from each UTC scheduler slot, so a restart within the slot replays the
same command; PostgreSQL's active-command uniqueness makes an operator-triggered or earlier fleet
command win without overlap. A later probe re-evaluates durable due state.

The current migration head is `0152`. Migrations `0128`–`0130` require a coordinated maintenance
rollout, not a mixed-version migrate-first deployment. Stop cadence dispatch; drain every old source
and ingestion-command worker; apply the migrations; and restart the API, scheduler, and workers as
one coherent version. Migration `0129` reconciles legacy terminal Pacer deferrals. Migration `0130`
intentionally expires live pre-heartbeat command leases once so only the new worker can reclaim
them; any undrained old worker is fenced from completion. Before cadence resumes, verify paused
source runs have no completion timestamp or lease state, each reconciled command is reclaimed only
once, and the new command worker renews leases during a source run. See
[Production operations](production-operations.md#entity-profile-pacer-and-command-lease-migration-rollout)
for the exact rollout and reconciliation sequence.

Migrations `0135`–`0137` add operator-safe run evidence, deterministic event semantics, and the
reviewed Meetup detail budget. `0135` stores only closed stage/outcome codes and bounded numeric
aggregates; it has no column for provider payloads, arbitrary log text, credentials, stack traces,
command lines, or lease tokens. `0136` persists deterministic topics/facets and conservative free
inference with field/rule provenance. `0137` advances only the exact reviewed Meetup San Francisco
and New York rows to 41 request units (one city page plus at most forty identity-checked event
pages), increments their source revisions, and leaves adapter identity and authority closed.
`0138` removes temporary-relation OID sensitivity from broad, paged topic browsing while retaining
the bounded 101-event scan, keyset order, and additive AND-topic contract.

Migrations `0139`–`0152` retain that coordinated posture while adding retired-source lifecycle,
run-link evidence, catalog sorting/facets, and the entity index/quality/history projections. The
`0152` image and schema must move together; rebuild the entity index once after cutover if any
source refresh ran under an older image during the migration window.

The HTTP request only persists a command. It never contacts an event publisher. A worker crash
leaves the command reclaimable, and each actual source refresh still uses the existing source/run
lease, policy recheck, pacing, approved-origin checks, and atomic catalog/provenance commit. If a
source attempt reaches a healthy but refilling/shared backoff bucket, the worker returns the same
command and stable run key(s) to the queue at a database-clock retry time. This applies to both an
exact-source refresh and a mixed fleet pass: already-queued sources remain idempotent while the
fleet command retries its deferred sources using the longest bounded Pacer delay. That transition
is lease-fenced, survives restarts, and is capped at five attempts for one source or fifty passes
for a due-source fleet; the worker never sleeps while holding the command lease.

The command lease is 300 seconds by default. While source work is active, the worker renews only
the exact live command/token pair every `min(60 seconds, lease / 3)`. A false renewal result or
renewal error means the worker no longer has authority: it cancels the in-flight provider operation
and does not write a completed or failed terminal command state. Expiry and the normal guarded
claim path determine the next owner.

The first control slice deliberately supports only:

- Refresh one existing reviewed source.
- Run one bounded pass over currently due sources.
- Pause or resume one reviewed source or a bounded selection of at most 100 matching sources. The
  bulk operation is all-or-nothing, checks each exact source revision, records an audit entry, and
  requires explicit review acknowledgement.
- Edit one reviewed source's seed URL, approved HTTPS origins, enabled state, refresh interval,
  minimum pacing, page limit, and optional review expiry. The save checks the exact source revision,
  advances the registry revision, and requires explicit review acknowledgement.
- View source admission/cadence state, bounded source analytics, safe refresh history, recorded
  build/configuration provenance, and command progress.

It cannot create or delete a source, change its source key, display identity, publisher, region,
adapter mode, or handoff-only authority, release a quarantine, or change fleet source policy.
Those remain migration-owner or separately reviewed control-plane operations. Enabling a source
does not bypass review freshness, approved-origin validation, the adapter's closed fetch contract,
or the fleet policy gate.

## Dashboard interpretation

`Active` means the registry entry is enabled, its review is current, and the fleet-wide
`public_jsonld/browser` policy admits collection. `Due` means its last successful refresh plus its
configured interval is in the past. `Paused` means the reviewed registry row is disabled and its
retained catalog remains visible until normal projection expiry or another owner action. A source
can therefore be both due and blocked.

The Overview intentionally distinguishes fleet-wide and bounded data:

- Registry, admission, due, running, failed-24h, pending-command, and catalog-event totals come from
  overview v2. Its running total is derived from normalized, unexpired source-run and command-lease
  facts rather than stale status labels. Its failed-24h total counts only the latest unresolved
  failed run for each source, so a recovered source and repeated historical attempts do not inflate
  the operator signal.
- Candidate, canonical-output, success, and duration values come from the currently selected,
  server-bounded run window and loaded page. They are labeled as loaded-window data and are not an
  all-time conversion funnel.
- The attention buttons drill into the matching source, run, or command ledger; the source rows
  open the existing end-to-end drawer rather than creating a second detail model.

Refresh command states are separate from source-run states:

- `queued`: committed and waiting for the command worker. A command may briefly return to this
  state while waiting for a safe Pacer retry.
- `running`: held by one live command-worker lease.
- `completed`: dispatch finished; inspect the result and source run. A result of `queued` means the
  source-specific Temporal workflow accepted the work, not that ingestion finished. A terminal
  `deferred` result means all five source attempts or fifty due-fleet passes remained paced; no
  unsafe source call was made.
- `failed`: the command worker recorded a bounded error category. The source ledger remains the
  authority for refresh failure and retry.

An old catalog row that still says `running` after its database lease expires is projected here as
`failed` with the safe code `lease_lost`; it is not counted as live work. A later guarded refresh
may reclaim that source/run slot.

Provider documents, credentials, lease tokens, tenant data, and raw provider error bodies are never
returned to the admin browser.

### Provenance interpretation

New exact-source admin commands snapshot the source configuration revision and accepting API
release identity at durable acceptance. Each later command-worker claim records that worker's
source/release identity separately. The stable `admin:<command UUID>` run key allows the safe run
projection to join these records while retaining the distinction. Existing runs remain explicitly
`legacy_unavailable`; the UI never assigns the current build to old work. Cadence/manual runs may
expose the source revision recorded by refresh progress while correctly leaving worker-claim
attribution unavailable.

`claim_recorded` means a specific worker durably claimed the admin command; it does **not** prove
that the same process performed a provider fetch. The router may safely queue a Temporal workflow,
observe an already-completed source run, or lose and later reclaim a lease. Accordingly, the UI
labels this evidence **Worker claim** and treats before/after charts as correlation only.

`make stack`, `make api`, and `make build` label local images with `git describe --always --dirty`.
A value such as `f3c5875-dirty` identifies the checkout but is intentionally marked mutable, and a
missing image digest means it is not immutable deployment proof. Production release automation must
supply both an immutable release revision and `sha256:` image digest for stronger attribution.

### Run-stage and resource interpretation

Every non-legacy run projects the same five ordered stages. Today only three boundaries have direct
timers; the other two remain explicit so the UI does not imply finer attribution than the worker
actually records:

| Stage | Current measurement boundary |
| --- | --- |
| Admission | Monotonic wall time around policy, review, and lease admission. |
| Collect | Adapter wall time, including transport, parsing, extraction, and bounded enrichment. |
| Extract + enrich | Logical stage included in the collect boundary; not separately timed. |
| Normalize + dedupe | Logical stage included in the atomic catalog-commit boundary; not separately timed. |
| Catalog publish | Wall time around the atomic catalog commit, including normalize and dedupe. |

Expanding a row in **Runs** renders a chronological execution log from a closed event vocabulary:
command requested, command claimed, latest recorded attempt started, measured stage observed,
execution aggregate observed, run status observed, and command completed. Each entry carries only
the fields appropriate to its evidence basis, such as a closed stage/outcome code, bounded duration,
or observation count. The log is explicitly marked incomplete because it orders retained lifecycle
and aggregate observations; it does not reconstruct unobserved retries or claim that aggregate
timestamps are a full causal trace. At most eleven entries are projected for one run.

Migration `0135` stores only closed, bounded execution evidence. Wall time is measured with a
monotonic clock and may aggregate repeated observations of the same stage execution. Shared Temporal
activity workers deliberately record wall time only: concurrent activities make process CPU and RSS
deltas misleading, so those values remain null with measurement scope
`activity_wall_clock_only`. The guarded direct/sequential worker may also record a best-effort
whole-process CPU-time delta and process boundary RSS samples with scope
`worker_process_boundary_samples`.

The displayed CPU ratio is process CPU time divided by wall time, **not** host CPU utilization.
Boundary-observed peak RSS is only the maximum of the first and last Linux process samples, not a
continuously sampled peak; process-lifetime peak RSS belongs to the whole worker lifetime. These
signals can reveal regressions and correlations, but they cannot prove that one stage caused shared
process resource use. Use host/container telemetry for saturation and capacity decisions. Legacy
runs and unsupported platforms remain explicitly unavailable, and telemetry write failure never
changes the ingestion outcome. No raw logs, provider payloads, credentials, stack traces, lease
tokens, or host filesystem paths are stored in this evidence model.

## Local source-policy opt-in

The repository intentionally seeds public catalog browser collection as fail-closed. The dashboard
will show `modality_disabled` until the local database owner explicitly enables it. Enabling this
policy authorizes outbound reads to every individually enabled and reviewed registry source; inspect
the source list before proceeding.

For the local Compose database:

```bash
docker exec events-concierge-postgres-1 \
  psql -U ec -d ec -v ON_ERROR_STOP=1 \
  -c "SELECT public.fn_set_source_policy(
        'public_jsonld',
        '{\"browser\": true}'::jsonb,
        false,
        false,
        'none'
      );"
```

Restore the fail-closed posture with:

```bash
docker exec events-concierge-postgres-1 \
  psql -U ec -d ec -v ON_ERROR_STOP=1 \
  -c "SELECT public.fn_set_source_policy(
        'public_jsonld',
        '{}'::jsonb,
        false,
        false,
        'none'
      );"
```

Do not treat this local command as a production authorization procedure. Production policy changes
require owner/legal review and the audited control-plane process in
[production-operations.md](production-operations.md).

## Historical local fixtures

Older development runs may have written `test-*` registry rows into the persistent local database.
The admin hides recognizable fixture sources by default and reports their count; the durable rows are
not silently deleted. Current integration tests use disposable databases and no longer add new
fixtures to the runtime database.

To get a pristine local catalog, use the existing destructive reset only after deciding that all
local application state can be discarded:

```bash
make reset CONFIRM=reset
make stack
```

## Operations

```bash
make stack                 # includes command worker + local cadence scheduler
make app-logs              # includes API, command-worker, and scheduler logs
make ingestion-commands    # run the worker outside Compose
make ingestion-cadence     # run the local enqueue-only cadence scheduler
make catalog-refresh SOURCE_KEY=luma-sf
make catalog-refresh SOURCE_KEY=luma-nyc
make catalog-refresh SOURCE_KEY=meetup-sf
make catalog-refresh SOURCE_KEY=meetup-nyc
make catalog-cadence
```

The source and cadence one-shot CLI commands remain useful for recovery and diagnostics. They obey
the same registry, policy, pacing, and run-lease gates as admin-submitted work. Outside Compose,
recurrence is opt-in through `EC_CATALOG_INGESTION_SCHEDULER_ENABLED=true`; its interval defaults to
300 seconds and is constrained to 60–3,600 seconds.

The generic reviewed-source editor cannot broaden the Meetup city adapter into an unbounded crawler.
That adapter still rejects redirects, unapproved origins, non-city profiles, noncanonical detail
URLs, authenticated requests, and application/member/attendee state. Its `41`-unit request envelope
is one exact city page plus at most forty deterministic, identity-checked event pages. Use the
[Meetup ingestion runbook](meetup-ingestion-runbook.md) for source-specific verification and
failure triage.
