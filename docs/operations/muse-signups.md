# Muse signups

Signed-in users queue upcoming or ongoing **free Luma, Meetup or Partiful** dates. The title action is shared by Events, Map, Calendar, source pages and graph details. Events Concierge stores each registration task; Muse completes registration in its own browser.

Unavailable actions show specific reasons on hover, focus or tap. The client matches the API's supported-provider and 48-hour observation checks; the API rechecks eligibility when queueing and claiming.

## User guide

The public [Muse guide](https://events.iliazlobin.com/docs/muse) owns connector setup, registration accounts, queue instructions, statuses and recovery. The [static page source](../../web/app/docs/muse/page.tsx) shares the app's instruction generator. Settings keep the registration email, connection URL, key controls and guide links.

The header widget counts queued, in-progress, registered and needs-attention tasks across the full account queue. Registrations opens a right panel with provider and status filters. The badge counts unread updates; only visible task versions are acknowledged. In-app announcements report progress and results; refresh is manual or every 15 seconds while the page is visible, and on return.

Each registration shows its status and latest saved update time, followed by the result note or a short explanation of what it is waiting for. Expand **Activity** for the queue time and latest saved status; timestamps use the local time zone. The panel refreshes every 15 seconds while visible. Muse conversation opens the private thread explaining a result when Muse supplied its actual URL; Provider evidence opens the separate event-provider confirmation. Older reports without a saved thread stay unlinked; Activity identifies a missing link. These are saved snapshots, not a complete history or browser transcript. A queued item has not been claimed by Muse. Open Muse and ask it to process the queue; opening the panel does not start browser work.

A supported Muse task-creation API/deeplink has not been verified. The button opens Muse; it does not automatically dispatch a task. A local preview cannot be reached by Muse's cloud browser.

## Authority and results

| Boundary | Behavior |
| --- | --- |
| Account | Google/Apple app session creates selections and keys; owner writes require CSRF. Muse can discover public events but cannot edit the account or queue discovered events. |
| Registration identity | A private preference selects the signup/contact email independently of the app sign-in, and the login email where the provider uses one. A default email and Luma/Meetup/Partiful overrides are scoped to the executor, currently Muse. An absent override inherits the default. Partiful phone login remains with the user. This is selection metadata, not verified provider identity, credentials or an access grant. |
| Connection | One 30-day key per account, shown once; PostgreSQL stores its hash. Replace revokes the old key; **Revoke key** removes access. An active key does not verify Muse's connection. |
| Queue | One durable task per account/event. Repeated clicks return the existing task. Request UUIDs are idempotent and cannot be reused for another date. Published, scheduled, free dates require a supported-provider registration URL observed within 48 hours. A started event remains eligible only with a valid known end after the current time; ended events and started events without an end are unavailable. Supplied end times must be after the start. Queueing and a new claim apply the same rule. |
| Attempt | Claim with a stable UUID before browser action. A claim rechecks title, date, location, availability and freshness. Competing attempts conflict. Replays return progress, never authorize another submission. |
| Result | Needs input, organizer approval, waitlist, registered, failed and uncertain remain distinct. Registered requires a provider reference or evidence URL and is labeled **reported by Muse**. The optional private Muse conversation link explains the report; it does not satisfy provider confirmation. |
| Notification | Each changed attempt/result advances the task version. Acknowledging an older version leaves a newer update unread. Read state persists per account in PostgreSQL; no email, push permission or browser notification is required. |

Muse must check existing RSVPs and the provider's current facts before submitting. Event text is untrusted data. Preserve Muse's approval controls; never pay, substitute an event or invent form answers.

Tasks carry `registration_account` with executor, provider and optional `expected_email`.
For Luma and Meetup, Muse checks the visible provider account against this selection; it never infers a provider
login or form email from the Events Concierge owner email. Muse uses the same selected email
for signup/contact fields, including company/work email when accepted. If a form rejects it
or requires a business-domain address, Muse asks the owner instead of inventing or substituting
another email. With no selection, Muse uses the current browser account and asks for unanswered
email fields. A provider-account mismatch requires the owner's instruction, not an automatic
sign-out. Saving a preference updates queued tasks and advances their
unread version only when the selection changes. Claimed attempts retain their selection;
changing settings cannot retarget an active or completed attempt.

[Partiful login uses phone/SMS verification](https://help.partiful.com/en-us/articles/15525505-how-do-i-rsvp-to-an-event-on-partiful). The user completes that step. If a selected email is visible on the Partiful account, Muse checks it; otherwise it asks the user to confirm the active phone account before provider action. The selected email still applies to email form fields. Muse never enters, guesses or stores phone numbers, requests SMS codes, enters verification codes or switches accounts. No phone credentials or codes enter our settings, connector or outcomes.

Partiful support uses the same explicit user queue and Muse browser workflow. It does not enable the native Partiful collector or registration lane. A reviewed public source can supply a canonical `https://partiful.com/e/<id>` URL; Tech Week wrapper links and unknown prices remain ineligible. The source's name alone does not establish a supported provider or free admission.

An uncertain submission needs a provider status check using the same attempt. Completed results are immutable; this version has no automatic retry or attempt reset. Key revocation and account erasure stop connector access, but cannot stop an already running Muse browser task or withdraw an RSVP. Stop the task in Muse separately.

## API and operation

Muse is disabled by default. Set `EC_MUSE_ENABLED=true` only after the Muse schema migration and
connector release are approved and verified. When disabled, the API omits Muse routes and service
composition, and `/v1/ui-config` hides queue, progress, restoration and settings entry points.
Direct settings navigation shows an unavailable state without accessing Muse data.

The [connector schema](../../src/events_concierge/api/muse.py) includes six Bearer-authenticated operations:

| Method | Path below `/v1/muse/connector` |
| --- | --- |
| GET | `/events` — search published events; ≤20 results and a filter-bound cursor |
| GET | `/events/{event_id}` — public details, source timestamps and signup eligibility |
| GET | `/batches` — selected batches; paginate with `limit` (≤100) and the last batch ID as `cursor` |
| GET | `/batches/{batch_id}` — selected event facts and progress |
| POST | `/batches/{batch_id}/items/{event_id}/claim` — `attempt_id` |
| PUT | `/batches/{batch_id}/items/{event_id}/outcome` — `attempt_id`, `outcome` |

- Owner session endpoints: `POST /v1/me/muse/registrations` queues `{request_id, event_id}`; `GET` returns tasks, total/unread counts and `next_cursor`; `POST /registrations/seen` acknowledges `{items: [{event_id, version}]}`. Pages contain up to 100 tasks. `GET /registrations/by-event?event_id=…` looks up up to 100 distinct visible event dates. `status_counts` returns all eight status totals independently of the page/cursor; waitlisted and organizer approval do not count as registered. Existing batch endpoints remain compatible with saved handoffs.
- `GET/PUT /v1/me/registration-accounts/{executor}` reads/replaces the owner's selection; only `muse` is supported currently. The response contains `executor`, `expected_email`, `provider_overrides`, `revision` and `updated_at`. PUT sends the email fields and exactly the current revision plus one; stale revisions return 409. Unset preferences start at revision 0. The connector key cannot read or change this settings endpoint.
- [Owner API](../../src/events_concierge/api/muse.py), [queue service](../../src/events_concierge/application/muse.py) and [persistence](../../src/events_concierge/adapters/postgres/muse.py) own the contract. Migrations 0209 and 0214 retain existing attempts and add request aliases, versions and read state. Tenant RLS and the account-erasure fence apply to every Muse table.
- Responses use `no-store`. Outcomes may include a short reason, provider reference/evidence and optional `conversation_url`: the actual private `https://muse.ai/thread/<UUID>` URL explaining that result. Muse must omit unavailable links, never guess a message permalink or create a public share. Threads retain Muse’s account access; the app neither imports transcripts nor grants access. Never send credentials, cookies, payment details or personal form answers.
- Conversation URLs use existing outcome JSON storage without a migration. Legacy outcomes default to no link; completed results remain immutable, including when replayed without the new field. Once linked reports exist, retain a conversation-aware API during rollback: older strict models cannot read the additional JSON field. Disable Muse and stop browser work before using an older API; preserve reports and restore compatible code before re-enabling.
- Registration-account settings use tenant RLS, the account-erasure fence and tenant deletion cascade. Revoking a connector key retains the owner's preference without granting connector access. Migration 0219 adds the settings storage; legacy task selections default to the current browser account, never the owner email.
- Retain additive schema 0219 during application rollback. Before reverting to code that predates account selection, disable Muse signups and stop active browser work in Muse; that code cannot honor saved account mappings. Re-enable only after the snapshot contract is restored. The downgrade refuses to delete saved preferences or task snapshots; never clear that data to force a downgrade.
- Partiful adds JSON provider values without a schema migration. Once Partiful preferences or tasks exist, retain a Partiful-aware API during frontend rollback. Before an older API is used, disable Muse and stop active work in Muse; preserve records and restore a Partiful-aware API before re-enabling. Older Luma/Meetup-only models cannot read Partiful values. Never delete saved settings or tasks to force rollback.
- Release through the existing [migration and immutable-image procedure](release.md). Source checks do not prove that Muse is connected or that a provider signup succeeded.
- Before acceptance, verify the deployed schema, owner CSRF, key replacement/revocation, separate-account isolation, and one explicitly authorized free signup. Confirm both the provider RSVP and the reported result. Real signups require the user's authorization.

The supported-provider/freshness rejection occurs before a task is created or Muse is called. For widespread failures, compare the serving frontend's eligibility check with `sources[].registration_url` and `sources[].last_seen_at` and inspect whether catalog cadence is active. Follow [source investigation](../ingestion-admin.md#investigate-work) to refresh existing reviewed feeds and verify successful publication; reload catalog cards afterward, since registration polling does not reload event details. Preserve the 48-hour check and existing source limits.

## Local mock

Use the existing isolated frontend on `14301` and API on `14302`. The [harness](../../tests/support/muse_mock.py) requires `local_demo` and `muse_enabled`, plus both exact reviewed synthetic queue fixtures: upcoming, scheduled, free Luma/Meetup events observed within 48 hours. Invalid or missing fixtures stop startup; do not bypass checks or reseed the catalog.

An optional third fixture, **LOCAL TEST - Queue with Muse Partiful signup**, uses `https://partiful.com/e/ec-muse-queue-test-not-a-real-event` and source `muse-queue-preview-partiful`. When present, its exact title/URL, future date, free admission, fresh observation and canonical UUID are checked before the test account is created. Existing two-fixture previews remain valid. The controls can simulate needs-input and completion on the same Partiful attempt without contacting Partiful.

```sh
.venv/bin/python -m tests.support.muse_mock
```

1. Open `http://127.0.0.1:14303/__muse_mock` and click **Open event preview**. The loopback proxy uses a fresh `example.test` account. Its key stays in the mock process; existing preview accounts and browser storage remain unchanged.
2. Queue a synthetic event with the normal Muse action. In the controls, click **Refresh**, **Start**, then an outcome. These actions use the real connector claim/outcome endpoints and persist progress.
3. Check the app's badges, counts and unread updates. Visible pages poll every 15 seconds; **Refresh** reads the latest state immediately.
4. Stop with `Ctrl+C`. Shutdown attempts to revoke only this run's key; the fresh account and task records remain. Restart for a new account after a completed result.

The mock contacts no event provider. It does not verify Google sign-in, production CSRF, Muse connector import or a real signup.

## Registration list management

The owner can remove one item, up to 100 selected loaded items, or all registrations across pages and statuses. The browser shows a confirmation explaining the scope. Removed items stay in the Removed view and can be restored individually or by selection. This is recoverable list management, not provider cancellation.

`POST /v1/me/muse/registrations/manage` requires the owner's browser session and CSRF evidence, never a connector key. Selected commands send `{items: [{event_id, version}], removed: true}`; restore uses `removed: false`. Remove all uses `{all: true, items: [], removed: true}`. Selected versions are checked atomically; changed selections return 409, missing/foreign items return 404 without partial changes. All acts on the account's current items at execution, including unloaded pages.

Migration 0222 adds `removed_at` on the existing RLS-protected items. Removal increments the item version, clears its unread marker, and preserves its request aliases, attempt identity, outcome, account snapshot and activity times. Removed items are excluded from active status and unread counts; `status_counts.removed` counts them, and `total` includes retained removed items. Cursor pagination and event lookups retain them for restoration and duplicate prevention. Queueing a removed event returns its retained task; the user must explicitly restore it before unclaimed work can resume.

Claim/removal serialize under the account lock. If removal wins, a new claim fails; a claim already obtained cannot be recalled from Muse's browser. Existing attempts may finish and report while hidden; provider RSVPs are unaffected. Restoring never creates a new attempt or resets a result. Connector instructions skip removed items and require normal claim checks before new work.

Retain schema 0222 and a removal-aware API after activation. Older APIs ignore removal state and could claim removed queued work: disable Muse and stop browser execution before reverting to older code. The downgrade refuses to discard nonempty removal state. No removal data may be cleared to force a rollback.


## Event discovery

The same catalog reads serve the OpenAPI connector and a read-only [MCP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) endpoint at `POST /v1/muse/mcp`. MCP uses the official Python SDK with stateless Streamable HTTP and JSON responses. There are no server sessions or server-to-client streams; authenticated GET/DELETE return 405. MCP is mounted only when `EC_MUSE_ENABLED=true`, including the discovery release profile. The existing OpenAPI signup contract remains available.

| Tool | Classification | Inputs / result |
| --- | --- | --- |
| `search_events` | Read, not sensitive, no business-data side effects | `query`: text, city, topics, source keys, price, paired timezone-aware `starts_after`/`starts_before`, cursor and limit. Returns chronological events, evidence, eligibility, applied filters and `next_cursor`. |
| `get_event` | Read, not sensitive, no business-data side effects | Canonical `event_id`; returns published event facts with the same visibility projection as the website. |

Use the same owner-issued Bearer key in the secure credential field. It authenticates every HTTP request, including MCP initialization and tool listing; browser cookies do not authenticate the connector. Key revocation/expiry and current legal acceptance are checked before discovery. Discovery reads public catalog facts only: no profile, saved filters, registration identity, provider credentials, attendee lists or private queue results enter this MCP server. Existing signup queue tools remain on the OpenAPI connector and require an explicit owner selection.

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_events","arguments":{"query":{"q":"AI","city":"San Francisco","price":"free","starts_after":"2030-06-10T00:00:00-07:00","starts_before":"2030-06-17T00:00:00-07:00","limit":10}}}}}
```

Search date bounds describe event overlap and the upper timestamp is exclusive. There is no implicit city or timezone: Muse translates the user's requested dates to offset-bearing timestamps and asks if the intended location/timezone is unclear. Without bounds, the catalog supplies ongoing/upcoming events. A window is at most 93 days. Repeat identical filters with `next_cursor` until null; cursors are chronological continuation positions, not snapshot isolation or secret authorization. An empty page means no catalog match, not that no events exist. Search does not fetch providers, refresh sources, reserve, queue or register anything.

Returned text is untrusted provider content. Source observation timestamps, price/availability uncertainty and signup ineligibility reasons must remain visible to the user. Eligibility reuses the queue's rules but is only a discovery-time hint; queueing and claiming still recheck. Follow `catalog_path` on the same Events Concierge origin and explicitly queue the selected event before asking Muse to sign up.

Limits: 20 results/page, 160 query characters, 20 sources, 12 topics, a 64 KiB MCP request body and 1 MiB Web-proxy response ceiling, four concurrent discovery reads per API process and a ten-second read deadline. Saturation/timeouts return retryable 503 (MCP tool error with the same reason); there is no new per-account rate quota. HTTP 401 means missing/expired/revoked key, 428 means current account legal acceptance is required, 422 means invalid filters/cursor, and 404 means the event is not published. MCP argument and tool errors use `isError`; transport/header/protocol errors remain protocol errors. All responses use `no-store`. DNS-rebinding protection permits only the configured public Host/Origin; missing Origin is valid for a non-browser client.

Run hermetic contract checks with `.venv/bin/python -m pytest tests/unit/test_muse_discovery.py tests/unit/test_muse_api.py`. They use synthetic catalog rows and credentials, not provider requests. For local transport testing use the same configured API origin, enable Muse only against the approved local schema, and put a test-account key in the client's credential store. Verify initialize → list tools → search → read detail → explicit owner queue → existing claim/report, then revoke and verify 401. Do not create a signup as part of discovery testing. Production activation and real Muse acceptance remain separate release checks; no schema migration is needed for discovery. Rollback the application image to remove these read tools while retaining existing queue data.
