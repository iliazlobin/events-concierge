# Ingestion administration

The operations console inspects backend queues and the reviewed public-event catalog, and submits
durable refresh commands. For the local fixture profile, start the complete stack, then open:

```text
http://127.0.0.1:3001/admin
```

The FastAPI-packaged fallback is available at `http://127.0.0.1:8000/admin`.
The consumer app and admin app are intentionally separate. Consumer tenant authentication never
grants operator authority. The consumer API retains the explicit local/mock admin guard; the hosted
operator API is a separate entrypoint.
A loopback `Host` is required on every admin request, and startup also rejects
`EC_ADMIN_INGESTION_ENABLED=true` unless the configured `EC_API_BIND_ADDRESS` is a loopback address.
Do not publish the Compose API port on `0.0.0.0` while using this local control room.
After migration `0182`, local admin uses `EC_OPERATOR_DATABASE_URL` and the command worker uses
`EC_INGESTION_EXECUTOR_DATABASE_URL`. `make migrate` explicitly bootstraps the local fixture logins
`ec_local_operator` and `ec_local_ingestion`; it never adds operator membership to `ec_app`.
Changing fixture passwords also requires matching URL overrides. Existing passwords are preserved.

## Local database unavailable

If all Overview reads return 503, check `/readyz` and the PostgreSQL container before retrying
individual admin endpoints. `No space left on device` during PostgreSQL recovery can mean the
Docker VM's data disk is full even when macOS has ample free storage. Check
`docker compose exec -T postgres df -h /var/lib/postgresql/data` and `docker system df`.
Reclaim only verified, unreferenced project build cache; preserve database volumes, running
containers and tagged release/rollback images. Do not reset the database to recover disk space.

On September 8, 2026, the local Colima ext4 data disk `/dev/vdb1` exhausted space available to
PostgreSQL. Its reserved-block setting was reduced from 5% (786406 blocks) to 1% (157281 blocks),
alongside scoped frontend build-cache cleanup, allowing PostgreSQL to recover without a restart.
The exact reserve rollback is `colima ssh -- sudo tune2fs -r 786406 /dev/vdb1`, once enough disk
space is available. This machine-specific setting leaves a reserve; it does not increase disk
capacity. Increasing this Colima VM's 60 GiB disk requires a coordinated VM restart.
Seven untagged, unreferenced Events Concierge API builds were archived on macOS before removing
their Docker image references. The archive is
`/Users/iliazlobin/Library/Caches/events-concierge/docker-recovery/20260908-admin-outage.tar.gz`;
all OCI blobs and image identities were verified by SHA-256. Restore with
`docker image load -i <archive-path>` when sufficient VM space is available. This is a local
recovery copy of build images, not a database backup.

## Hosted operator boundary

Run `uvicorn events_concierge.api.operator:create_operator_app --factory` in its own process with
`EC_OPERATOR_API_ENABLED=true`, a dedicated operator database credential, an exact HTTPS
`EC_OPERATOR_PUBLIC_ORIGIN`, `EC_OPERATOR_IAP_AUDIENCE`, and `EC_OPERATOR_SUBJECT_ROLES` (a bounded JSON
map of IAP subject to `viewer`, `operator`, or `reviewer`). Google IAP's original signed assertion is
verified again by the API. Unsigned identity headers and consumer cookies confer no authority.
Mutations require JSON and the exact configured browser Origin. The receipt actor is derived from
the verified subject.

- **Viewer:** inspect catalog, commands, aggregate backend state, and restricted work diagnostics.
- **Operator:** viewer capabilities plus refresh and optimistic-concurrency pause/resume.
- **Reviewer:** operator capabilities plus reviewed configuration edits. Adapter mode is immutable;
  new adapters and source onboarding remain reviewed migration work.

The dedicated database pool validates the actual login's `ec_operator_controller` membership and
rejects consumer/elevated authority. Migration `0182` revokes admin function access from `ec_app`;
`ec_operator_viewer` holds safe projections, the controller inherits it, and `ec_ingestion_executor`
has explicit catalog and command-lease capabilities. Production LOGIN principals, passwords, and
memberships are provisioned separately. No role is implicitly granted to the shared consumer login.
The fixed backend aggregates use a separate `ec_operator_aggregate_definer` NOLOGIN role with
SELECT-only table access and an explicit SELECT policy for the FORCE-RLS erasure table. Runtime
logins cannot inherit it. Migration-owner ADMIN metadata on PostgreSQL 16 has neither SET nor
INHERIT authority; it supports later owner-managed migrations.

Migration `0185` adds the fixed `fn_get_operator_errors_v1` read for request-start and entity-refresh
diagnostics. The same restricted definer owns it; viewers receive function execution, never queue
table access. Responses contain opaque record references, scheduling fields and allowlisted failure
summaries/codes. Raw exception text, tenant identities, request payloads and lease tokens remain
outside the operator projection. Unclassified errors offer a reference for investigating restricted
worker logs. These reads do not retry work, reschedule records or establish worker liveness.

Migration `0188` adds `fn_get_operator_records_v1` and the read-only `/operations/records` route
for pending request starts and pending/terminal notification records. Request error filtering stays
within pending requests. Notification IDs remain exact signed decimal strings, and failure classifications
are allowlisted. The projection uses the existing restricted definer and exposes no payload,
recipient, tenant identity or mutation capability. Migration `0189` extends reference lookup to
the full signed bigint range used by retained notification records.

Migration `0190` adds `fn_get_operator_run_catalog_records_v1`, exposed by the existing read-only
`/admin/v1/ingestion/sources/{source_key}/events?run_key=…` route. Source, exact run and search filters
apply before the distinct-record count and keyset pagination. **View published records** in a run
opens this scope; `store_source` and `store_run` preserve it with the event search and cursor.
These are current canonical records whose retained source observation still names that run,
including past events. Later refreshes can replace attribution and canonical fields can change;
new inserts versus updates and historical event versions are not recorded. The count therefore
need not equal the run's original published count. The restricted definer owns this fixed read;
operators gain no raw table access, tenant data or mutation capability. Normal source browsing
keeps its existing latest-successful-projection semantics.

Migration `0191` adds `fn_get_operator_catalog_records_v1` and read-only
`/admin/v1/ingestion/events`. It lists retained published canonical records across sources,
with source/run/date/price/search filters, exact matching totals and source contributions in one
statement. Responses are bounded to 100 events and 500 source contributions; truncation is explicit.
The restricted definer and existing operator capability boundaries remain unchanged. This operator
inventory includes retained historical/paused-source/cancelled records, separately from consumer
eligibility. Existing source-event API links remain compatible.

The Helm `operator.enabled` profile creates the IAP frontend, isolated API, ingestion executor,
separate secret mounts and identities, and a deployment-owned cadence CronJob. See the
[chart deployment guide](../deploy/helm/events-concierge/README.md). This is source wiring, not evidence
of an activated IAP edge, migrated runtime, running workers, or completed source publication.

## Operator workspaces

The admin has five primary destinations: Overview, Sources, Catalog, Runs and Commands. Run
history and record investigations open contextually and retain stable URLs and browser history.

- **Overview** shows current source freshness and upcoming unique catalog inventory, with a
  direct link to run statistics. Background work stays visible with five entries per page,
  Previous/Next controls. Records and errors expand beneath their responsibility, keeping the
  summary and other work visible. One investigation is open at a time; changing the background
  page closes it. `ops_work_page` preserves its page across reload and record drilldowns;
  repeated responsibilities are shown once. There is no All work inventory or
  architecture map. One alert names unavailable Work, Sources and Catalog reads; Retry re-reads
  only failed services and recovers a failed admin session. The Catalog overview snapshot is shared
  with the console instead of requested twice. Successful partial measurements remain visible;
  missing snapshots remain unknown. Counts do not establish runtime availability
  or capacity. Architecture stays in the
  [canonical System Design](https://app.notion.com/p/391d865005a88164a182eabc18fe068f).
- **Run statistics** (`tab=run-stats`, labeled Runs) has the collection activity chart. It supports
  24h/7d/30d, all sources or one source, and Runs or Records. Totals come from the displayed server
  buckets; individual intervals expand matching runs beneath their table row. **Browse runs**
  expands the whole displayed range in place. Inline lists show ten runs per page with Previous/Next
  controls. Run details open below the inline ledger, with
  source, catalog and command references retained. Exact interval bounds and inspection state
  survive reload and browser history; changing the chart source or window closes the expansion.
  A saved interval outside the refreshed rolling chart remains available below the table. The data table is
  always visible and omits intervals with zero runs; actual runs with zero output remain visible.
  Empty intervals retain their position on the chart time axis but have no inspection control. Fleet buckets are
  hourly for 24h and daily otherwise; source history uses four-hour buckets for 24h. Published counts
  are repeated per-run observations, not unique catalog growth. Chart scope persists in
  `trend_window`, `trend_source` and `trend_metric`; Back to Runs and browser Back/Forward restore it.
- **Record investigations** expand within Overview with `ops_queue`. Request starts show Pending or With errors;
  Notifications show Pending or Failed history. Terminal failures are separate from pending work,
  and notification work can include audit records and quarantined projections. Rows expand to show
  safe error classifications, retry/lease timestamps, attempt measurements and exact references.
  Records use ten-entry pages with Previous/Next controls; expanding an item keeps its list visible.
  `ops_record` opens a record independently of its list page (`ops_offset`); `ops_scope` preserves
  the filter. Collapsing clears the open investigation; browser Back/Forward restores selection
  and filters without leaving Overview. Old queue
  and record links remain usable; `ops_work=1` alone now opens Overview. Retired view parameters
  are removed on navigation. Other known unresolved-error evidence appears within Background work:
  entity refresh opens exact source errors; remaining lifecycle failures link to the
  operations runbook. Quiet queues and retained ingestion-command failures are omitted. Failed
  reads are unavailable, never empty results.
- **Catalog** (`tab=catalog`) opens one paginated event list across all sources and dates. Search,
  Source, Dates (all/upcoming/past) and Price filters apply before canonical deduplication and
  pagination. Upcoming includes ongoing retained records and does not imply registration availability.
  Each row shows its latest matching source observation; event expansion opens exact source/run
  provenance. Sources and Runs links apply their filters to this same list. `store_source`,
  `store_run`, `store_dates`, `store_price`, `store_query`, `store_event` and the paired cursor
  preserve investigations. Legacy source-picker parameters are ignored and removed on new links.
  Its compact overview shows matching unique events, upcoming matches and contributing sources
  once, alongside the publication chart. The Source dropdown is the sole source-filter control;
  there is no parallel source ranking. Open an event through its title; its provider page and
  publishing run are available in the expanded details. The 24h/7d/30d publication timeline follows the
  source and `catalog_trend` window independently of event search/date/price filters. Points open
  exact run intervals. This is repeated collection output, not historical inventory growth:
  retained observations do not preserve past inventory snapshots or removed-record history.
  **Collection freshness** reads the complete current source roster independently of event
  filters and the chart window, or the selected source when one is chosen. It shows eligible
  sources due for refresh, recorded as collecting, awaiting first success, the oldest successful
  collection, and an **Upcoming collection** forecast for 24 hours, 48 hours or 7 days. Select a
  timeline interval to inspect its sources, search by source or publisher, or switch between
  next sources, already-due sources, recorded collection and missing due dates. The list includes
  each source's next known eligibility once, with local times, latest-run outcome and links to
  its configuration; Show more reaches the complete matching set. Refresh schedule re-reads the
  roster independently of events. Paused, retired and review-blocked sources do not contribute
  schedule dates. Eligibility is not a guaranteed run start or a prediction of repeated future
  runs; source collection timestamps are not individual event-update timestamps. Failed or
  incomplete roster reads leave this summary unavailable without hiding catalog records.
  **Manage source** opens the source's Configuration tab. Legacy `catalog_config` bookmarks still
  redirect to the corresponding Sources row. Missing statistics never replace event data with zeros.
- **Pipeline** remains available through existing bookmarks, outside primary navigation. It shows
  current source failures, cadence freshness and retry signals, with
  direct links to each source workspace and its runs. Run activity opens an exact start-time
  interval, or just the failed runs in that interval. Fleet activity excludes fixtures and its
  links preserve that scope. The first fleet bucket starts at the SQL grid boundary; the current
  bucket ends at the snapshot. Measured stages open their slowest runs or last failed stage
  outcomes. Stage and inspector selection persist in the URL. Source-specific views identify
  unavailable aggregate stage timing explicitly; fleet timing is never substituted for a source.
- **Sources** uses the full table width, with health, current catalog count, latest run timing,
  claims, output, errors, last success and next collection time. Above its filters, **Sources over
  time** shows the retained registry's cumulative registrations and additions over 7, 30 or 90 days
  (default 90), using database registration dates. This aggregate includes paused and retired
  sources; only the fixtures toggle affects its scope. It does not reconstruct deleted sources or
  historical enabled/healthy counts. Migration `0192` provides the read-only
  `/admin/v1/ingestion/source-registration-history` projection, including the starting balance and
  every daily interval. Hover or keyboard navigation reveals UTC counts; `source_trend` preserves
  the window through reload and Back/Forward. Table filters remain independent below the chart.
  **Current collection state** sits alongside registration history and uses the same exclusive
  state classification as source rows: retired/paused, review blocked, running, failed, due,
  deferred, no runs, then healthy. Healthy means reviewed, enabled, not due, with a successful
  latest run. These counts use a complete global roster, independently of table filters; a failed
  or truncated read cannot produce zero counts. Selecting a state clears table filters, preserves
  fixture scope and opens the corresponding `registry_lens` in one browser-history step.
  Current state is not projected into past dates: historical health snapshots are not recorded.
  Publisher and adapter filters live in the filter bar; row labels are plain metadata.
  **All events** and **Upcoming events** are adjacent, sortable Catalog counts. Both count distinct
  retained canonical events with Catalog's source/observation/run fixture exclusions; upcoming
  includes ongoing records (`coalesce(end_at, start_at) > now`) and retained cancelled events.
  Each count opens Catalog with that source and the corresponding Dates filter, clearing prior
  search, price, run, event and cursor state. Back/Forward restores the Sources filters and sort.
  Migration `0193` supplies both aggregates before sorting/pagination; `catalog` sorts upcoming
  and `catalog_total` sorts all events. Missing counts remain unknown rather than using the legacy
  active-future `event_count` field. Latest-run links open their exact investigations. Compact charts
  summarize the loaded matching registry's latest recorded outcomes, error codes and
  output. Chart selections filter the table through `registry_lens`; output bars expand a source.
  These are current/latest snapshots, not fleet historical throughput or an availability SLA.
  Selecting a source expands details immediately below its row. Overview keeps the collection
  schedule; editable settings appear once in Configuration. Source-scoped 24h/7d/30d/90d history
  defaults to 30 days with line charts of measured intervals: 1h for 24h, 2h for 7d, 6h for 30d
  and 24h for 90d. `source_window` preserves the selection across reload and browser Back/Forward.
  Quiet intervals stay on the time axis; point links preserve the exact start interval in Runs. The expansion
  includes recent runs and reviewed cadence, pacing, limits and endpoint configuration. The
  Configuration tab reuses the revision-checked reviewer editor; stale or denied exact reads block
  changes. Registry filters, sorting, chart filter and `source_selection` / `source_inspector`
  expansion state persist in links. Operators can select visible eligible rows and apply audited
  pause/resume changes to at most 100 sources at once. Queue refresh, source-page links, actionable diagnostics and technical identity live in the same expansion.
  The first source page renders while remaining pages load, with loaded/total counts and bulk
  writes disabled until collection completes. New searches abort superseded reads. Facet failures
  have their own retry and do not hide loaded source rows. Roster traversal is bounded to 10 requests
  and 1,000 sources; overlapping pages advance by the server's row count rather than unique rows.
- **Run history** (`tab=runs`) searches and sorts the full matching history before server pagination. Source, outcome,
  stage, last stage outcome, rolling window and anchored start interval constrain that history.
  `run_after` / `run_before` select an inclusive start and exclusive end, overriding the rolling
  window; `run_stage` / `run_stage_outcome` select recorded stage evidence. An exact bookmarked
  source/run is read independently of ledger page and filters. Previous/next controls and arrow
  keys move through loaded runs while retaining the inspector tab. Selectable stage duration
  profiles open measured observations; Execution offers a filterable, expandable evidence timeline.
  `run_evidence_stage` and `run_timeline` preserve those inspector choices separately from filters.
  Opt-in **Follow active run** refreshes every five seconds while visible, without overlapping
  reads, and stops on a terminal outcome or read failure. It follows retained evidence, not raw
  worker logs. Search, sort, selection and pagination survive links and browser history.
  Failed-run summaries show the recorded error category and stage, with expandable worker events
  and copyable log context. Events are matched to the exact source and run within the linked
  command's newest 100 events; older-attempt errors are labeled separately. This bounded stream
  does not contain exception messages or stack traces. Missing evidence stays unknown.
- **Commands** is an independent primary workspace, linked from accepted refresh receipts and run details, and follows
  accepted operator work separately from source execution. It supports
  a URL-addressable command investigation. Track command opens the exact accepted receipt, even
  when it is outside the newest 100 rows. The investigation separates command claims, source-task
  claims, source-run outcomes, lease heartbeats and observed progress. Attempt and source/run
  selections survive reload and browser navigation. A compact receipt queue remains alongside
  the selected command, with Overview, Activity and Execution tabs for progressive inspection.
  The page Refresh updates both receipts and selected evidence, including when following is
  paused. The selected header shows the command action and dispatch status once; request/page
  totals live in Activity.

These workspaces follow System's compact headers, restrained selection styling and contextual
actions. At narrow widths the inspector follows the main view and selecting an object brings it
into focus. Failed refreshes retain the previous snapshot only within the same evidence scope;
changing a source, window or page does not relabel earlier data. Authorization failures clear
protected snapshots.

The command investigation follows a bounded stream of structured operational events, with pause,
manual refresh and a retained stale snapshot during transient failures. It opens at the newest
recorded events and then follows forward; the browser retains at most 500 events. Stage start/finish events
come from actual execution boundaries. BiblioCommons, Meetup city, Luma calendar and Luma discover
also report throttled collection progress: completed approved reads, parsed list/feed pages and
collected candidates. These counts are cumulative within one task execution; unsupported counters
remain unavailable. Process logs carry the same command, command-attempt, source/run, task-attempt,
worker and build identities. This stream contains no provider bodies, credential material or raw
exception messages. It does not reconstruct events from before instrumentation was installed.

Selecting a source/run exposes its normalized run state, measured stages, qualified resource
samples and code ownership references. The current registry's code paths are navigation evidence;
only a recorded executing-worker identity identifies the build that performed an operation. A
local dirty revision is unpublished and cannot be represented as an exact remote source link.
Temporal handoff and source publication remain distinct; the dispatcher build does not establish
the build of a later Temporal activity.

Tabs, source, run window, and run status are encoded in the URL so an investigation can be shared or reopened.
For example, the Luma Bay Area Discover source can be opened directly at:

```text
http://127.0.0.1:3001/admin?tab=sources&source_selection=luma-sf
```

Sources searches display name, publisher, source key, region, adapter and seed URL. Each selected
source expands beneath its table row. Overview includes historical collection charts, recent-run
links, source settings, actionable diagnostics, source-page links and collapsed technical identity.
Configuration uses the shared reviewer editor for endpoint, approved origins, collection state,
cadence, pacing, fetch bounds and review expiry. Identity, adapter and policy remain managed.

**Queue refresh** submits a durable collection command and offers **Track command**. It requires
operator capability, a successful current source read, available/admitted fleet policy, a reviewed
and enabled non-retired source, and no active source run or submission. **Refresh source details**
only reloads displayed evidence. Source pause/resume failures stay visible through automatic reads.

Retired full-page `source=…` links reveal the corresponding Sources row and clear conflicting
registry filters. An exact bookmarked source remains inspectable when missing from a roster read;
refreshing registry membership preserves an open draft. `/admin/review` now redirects to Pipeline,
retaining a supported run window. The old source page, redesign preview, and unused inline versions
of Overview, Pipeline, Runs and Commands have been removed. The separately packaged FastAPI
`:8000/admin` dashboard remains the documented fallback for installations without Next.js.

### Daily collection and historical evidence

The starting operational policy is daily collection of upcoming events, with a 90-day event-date
window adjustable per source from 1 to 90 days. Cadence is an interval after the last successful
completion, not a fixed daily wall-clock appointment; dispatch, pacing and retry gates can add
delay. On September 8, 2026, the local registry's 91 enabled, reviewed sources that ran more often
were changed to 1,440 minutes through revision-checked, audited configuration updates. The remaining
enabled source was already daily. This gives 92 nominal refreshes/day, versus 391 in the existing
`research/source-cadence-analysis.ipynb` snapshot. These are schedule estimates, not HTTP request
counts or proof of completed daily runs. The notebook remains a dated historical snapshot.

Migration `0187` adds `collection_horizon_days` and captured execution settings. New durable runs
freeze a database-timed interval `[start_at, end_at)` and its source revision; retries reuse those
bounds. Each attempt also records the admitted cadence, pacing, page bound, review state and source
revision. A later source edit can change retry execution settings without silently moving its
original event window. Runs displays the frozen window, captured attempt settings and current
registry configuration separately. Older runs with no captured settings remain **Not recorded**.

The event window is not a retention limit. Existing historical catalog records and run evidence
remain available. Omission affects current source membership only when a successful collection
covered that event's start time; narrowing a window must not withdraw out-of-window events or
resurrect an event omitted by a prior covering run. Reviewed provider date parameters constrain
collection where supported, detail enrichment skips out-of-window events, and publication/staging
also enforce the frozen interval. A feed without reviewed date-query support may still require
listing traversal. This does not add universal past-event backfill to future-only adapters.

Use source history's collected/published observations, run duration, failures and retained stage
evidence to tune cadence and window. Repeated observations are not new events; new/changed/unchanged
event counters are not measured by this increment. Existing pre-`0187` staged pages with no captured
window cannot safely be resumed under an invented bound. An already expired frozen window also
requires a fresh run. Both cases release the owned lease, retain the window/staged evidence and
park with a source-changed outcome; use a fresh manual source run to resume collection.

The September 8 local rollout applied `0187` after stopping cadence/command dispatch and verifying
that no Temporal workflows were running on the old queue. The matching API, command worker,
scheduler and separate transactional/catalog Temporal workers were restarted together. A direct
Luma verification run published three records and a Temporal Alameda Legistar run published five;
both retained exact 90-day windows and their 1,440-minute execution cadence. These verify the local
execution paths, not a complete day of fleet collection or a production deployment.

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
    ├─ refresh_source ─► fixed one-source plan
    └─ refresh_due    ─► fixed bounded due-source plan
                              │ one eligible source per claim
                              ▼
                  mode-aware catalog refresh router
                              │
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
command win without overlap. A later probe re-evaluates durable due state. In the hosted operator profile the chart's existing
CronJob is the sole recurring authority: it invokes `ingestion_cadence --once` with controller
credentials. A production recurring daemon is rejected. `EC_INGESTION_EXECUTOR_ENABLED=true` selects
the separate catalog-only command/Temporal execution graph; it requires its dedicated executor URL,
shared Redis pacing, Temporal configuration, and GCS object storage, without the consumer provider
graph. Its explicit catalog payload converter accepts catalog workflow identities only and uses
the dedicated `EC_GCS_CLAIM_CHECK_PREFIX` under `events-concierge/catalog/`; the tenant converter
rejects catalog references. Local catalog payload files live under the `catalog` subdirectory.
Accepted commands and linked publication outcomes remain distinct; a successful cadence Job
proves enqueue evaluation only. Temporal Schedule cutover remains a separate deployment decision.

Local Compose runs `catalog-workflow-worker` with the same isolated executor credential and catalog
payload converter as `ingestion-commands`, on `events-concierge-catalog`. `workflow-worker` serves
the transactional queue. A combined tenant worker cannot consume externalized catalog payloads or
call the executor's window-preparation capability. Drain pre-cutover catalog workflows on the old
queue before switching. The matching host CLI entry points are `make catalog-workflow-worker`,
`make catalog-refresh`, and `make catalog-cadence`; they use the executor login as well.

Migration `0183` supports a command worker that freezes the first selected source
plan, persists each source task's disposition, and runs at most one source per command claim. A
continuation releases the parent lease and returns to the queue with a new availability time.
Eligible commands are ordered by availability, then original request time, so already-waiting
manual work can run between fleet sources. A manual command can still wait for the current source
to finish; this change does not introduce unbounded provider concurrency. Deferred source tasks
retain their exact run keys, and completed tasks are not re-executed. Retry budgets apply to each
source task (five for an exact-source request, fifty for a fleet source), independently of the
number of successful continuation claims across the plan.

For an existing active command, the new worker adopts its latest previously linked plan once and
labels that adoption explicitly. It preserves recorded successful source outcomes and does not
invent historical claim times. Command-attempt transitions and lease renewals are observed in the
database; structured progress records are separate from heartbeats. Source run expiry is normalized
consistently in both the command and run projections.

Before activating `0183`, stop/drain cadence and ingestion-command workers, apply the migration,
then deploy the matching API and worker version. The migration preserves operational evidence and
does not provide an automatic destructive downgrade. Source implementation and isolated validation
do not establish that the long-lived local or hosted database has been migrated.

Migration `0184` adds viewer-only bounded run query and exact lookup functions. Search and sorting
precede pagination; detailed execution evidence is enriched only for the selected page or exact
run. The query accepts at most 100 rows, 160 search characters and a paired timezone-aware interval
of at most 90 days. Stage duration sorting requires a stage. The matching API and frontend need
this migration to use the new drilldowns; source validation does not activate it in a runtime.

Migration `0186` optimizes the private per-run fixture predicate used by source lists, facets and
overview queries. It retains the predicate's semantics, identity and permissions. On September 8,
2026, the function replacement was first applied as a targeted local performance repair while
Alembic remained at `0182`. The later coordinated local rollout applied the migration chain through
`0187`, including the command/run projections and the same predicate definition. Registry reads now
also abort superseded requests and render the first page before the complete roster arrives.

Migrations `0128`–`0130` require a coordinated maintenance
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
source attempt reaches a refilling/shared backoff bucket, its source task receives a database-clock
retry time. The command can continue with other eligible tasks from its fixed plan before retrying
that source. If all remaining tasks are deferred, the parent yields until the earliest bounded
eligibility time. Already-completed or workflow-queued tasks remain idempotent. Every transition is
lease-fenced and survives restarts; provider retry waiting does not hold the command lease.

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
  advances the registry revision, and records explicit review acknowledgement through Save. Authorized
  reviewers edit fields directly in Configuration; Cancel restores the latest verified values.
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

Operations and Catalog browser checks run against a running Next.js frontend, with all operator API responses
intercepted by read-only fixtures:

```bash
EC_ADMIN_WEB_URL=http://127.0.0.1:3001 uv run python -m pytest \
  -o addopts='-ra --import-mode=importlib' -q tests/e2e/test_admin_operations.py
```

```bash
make stack                 # includes ingestion-command worker + local cadence scheduler
make app-logs              # includes API, command-worker, and scheduler logs
make ingestion-commands    # run the command worker outside Compose
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
