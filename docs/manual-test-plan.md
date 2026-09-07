# Manual UI acceptance test plan

This plan exercises the local consumer product as a person would use it. It covers the current
Chat/Events/Map/Calendar catalog experience, onboarding, discovery, durable requests, plans,
handoffs, preferences, session behavior, degraded Temporal behavior, and account erasure.

The primary target is the repository's local mock-cloud Next.js application at
[`http://127.0.0.1:3001`](http://127.0.0.1:3001). The older request/lifecycle journey below still
uses the FastAPI fallback at
[`http://127.0.0.1:8000/app`](http://127.0.0.1:8000/app). Neither surface proves production OIDC,
real provider mutation, email delivery, Google Calendar cleanup, or cloud data deletion.

## Test shape

| Pass | Approximate time | Coverage |
| --- | ---: | --- |
| Quick smoke | 10–15 minutes | Startup, onboarding, navigation, preview, durable request |
| Full journey | 25–45 minutes | Pick feedback, lifecycle views, settings, responsive and keyboard behavior |
| Resilience | 10–15 minutes | Intentional Temporal outage and queued-request recovery |
| Account erasure | 5–10 minutes plus worker wait | Destructive disposable-account test; always run last |

A required check passes only when both the visible result and the expected durable behavior match.
Mark data-dependent Plan or To-do checks `NOT EXERCISED` rather than passed when the workflow does
not produce that outcome. Record screenshots, the failed step, approximate time, and relevant log
lines for every failure.

## Safety rules

1. Use a private/incognito browser window and a unique disposable address such as
   `manual-ui-20260723@example.test`.
2. Use the complete `make stack` profile. `make api` intentionally omits the workers needed for an
   end-to-end request.
3. Keep account erasure until the very end. It permanently fences that local tenant.
4. In local-demo mode, **Leave this session** only removes the browser's tenant reference; it does
   not delete server data.
5. Do not run `make reset CONFIRM=reset` or `docker compose down --volumes`. Both delete local
   persisted state.
6. `Ctrl-C` while following logs stops the log view only. It does not stop the services.
7. Port `8234` is Temporal's operator UI. The primary consumer product is on port `3001`; port
   `8000` is FastAPI and the legacy static fallback.

## Test record

Copy this into the test report before starting:

```text
Tester:
Started at:
Browser and version:
Desktop viewport:
Mobile viewport:
Local image/version response:
Disposable email:
Tenant ID:
Durable request ID:
Overall result: PASS / FAIL / PARTIAL
Notes and screenshots:
```

## 0. Preflight

The stack is currently running. On a later run, start it only if `make ps` shows missing services:

```bash
cd /Users/iliazlobin/Claude/events-concierge
make ps
docker compose --profile app ps -a migrate
# Only when the full stack is not already running:
make stack
```

Expected:

- PostgreSQL, Redis, Temporal, FastAPI, and the Next.js web process are healthy.
- The seven durable workers are running.
- The one-shot `migrate` container may show `Exited (0)`; that is success.
- No service is repeatedly restarting.

Check the local contracts:

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:8000/readyz
curl -sS http://127.0.0.1:8000/v1/ui-config
curl -sS http://127.0.0.1:8000/versionz
```

Expected:

```json
{"status":"ok"}
{"status":"ready","components":{"database":"ready","temporal":"ready","identity":"not_configured"}}
```

The UI configuration must contain `"local_demo":true`. `identity:"not_configured"`,
`"release_revision":"development"`, and a null image digest are expected only in this local profile.
They are not production release evidence.

In a second terminal, follow application logs:

```bash
cd /Users/iliazlobin/Claude/events-concierge
make app-logs
```

Idle cycles with zero claimed work are normal. Investigate a new traceback, a repeatedly failing
pass, `lost_leases` above zero, or a container restart.

### Current unresolved persistent-data invariant

At 2026-07-23T04:10:01Z, the existing local database reported this unresolved global
lifecycle-invariant failure:

```text
scanned_nonterminal_workflows=7800
closed_nonterminal_workflows=7798
invalid_lifecycle_workflow_identity_count=477
active_lifecycles_missing_watch=111
requires_attention=True
```

This warning does not by itself attribute a failure to the newly created manual-test tenant, but its
root cause has not been established. Do not describe this persistent database as invariant-clean.
Track this as a separate defect and record any new warning tied to the new tenant or request
separately. The UI journey may receive its own PASS, but this runtime cannot receive an overall
release-quality PASS until the invariant drift is explained or removed.

Optionally run the non-product-state local canary before opening the browser:

```bash
make smoke-local
```

Expected: top-level `"status":"passed"` and 34 passed checks. `"release_eligible":false` is
intentional for a local-only canary. The `smoke-local` target first reconciles/builds the full stack,
so it preserves volumes but may recreate containers; never run it during an active manual workflow.

## Current catalog browse acceptance — primary port 3001

Open `http://127.0.0.1:3001` in a private window. Use DevTools **Network**, filter to
`/v1/catalog/events`, and preserve the log. These checks read the persisted current catalog; they
must not issue a provider request or start a catalog refresh.

### Shared filters and fixed layout

- [ ] **Events**, **Map**, and **Calendar** share the same query and filters when switching views.
- [ ] On a desktop viewport, source, place, price, date range, and Reset stay in fixed tracks.
  Selecting a longer city/area/source label does not move the neighboring controls.
- [ ] At a narrower supported viewport, the filter rail scrolls rather than compressing labels into
  ambiguous or overlapping controls.
- [ ] **Today**, **This week**, **Weekend**, and **This month** each update the one date window and
  produce one request with both `starts_after` and `starts_before`.
- [ ] Opening **Dates** shows one start/end range component. Selecting start and end changes only
  the draft until **Apply** is selected; **Clear** returns to the documented default window.
- [ ] Reusing Back/Forward or switching view does not pair a cursor from an old filter with a new
  filter.
- [ ] Move Calendar to the previous month. The request preserves the selected source, sends the
  local month start as `starts_after` and the next local month start as `starts_before`, and omits
  the previous month's cursor.
- [ ] A past month may return events when they remain in the source's latest successful projection.
  Clearing the custom range returns to the future-only default and does not surface elapsed events.

### Source and additive place semantics

- [ ] The source selector's **All sources** metadata says `N sources`; each individual source row
  says `N events`. These are different units and are not expected to add up as source counts.
- [ ] Selecting one source sends one `source_key` and narrows all three browse views. The current
  source control is single-select; mark multi-source selection as `NOT IMPLEMENTED`, not passed.
- [ ] With multiple places selected, change to a source that cannot match one of them. Compatible
  places remain selected, incompatible places are removed, and the next request does not retain a
  stale place filter that hides the newly selected source.
- [ ] The place selector groups **Areas**, **Neighborhoods**, and **Cities**, is keyboard operable,
  and keeps the menu open while adding or removing choices.
- [ ] Select two cities. The next request repeats the `city` parameter and results may match either
  city; adding the second city does not replace the first.
- [ ] With one city still selected, add a named scope such as **Bay Area**, **Manhattan**, or
  **Los Angeles area**. The request retains the city and adds `location_scope`; the location result
  is their union.
- [ ] Search for and select a city or named scope through the main smart-filter search. It is added
  to the existing place selection rather than replacing it.
- [ ] Searching the place selector for an unknown free-text location says no known place matches.
  It does not invent a city, bounds, or geocode.
- [ ] **Los Angeles area** includes reviewed cities such as Los Angeles and Santa Monica, while
  **Manhattan** uses its bounded neighborhood contract. Results outside the selected union do not
  appear.
- [ ] On Map, changing the place selection fits the selected event locations. If there are no
  matching points, it fits the selected named scope's fallback bounds rather than leaving the map
  centered on the previous city.

### Price and query semantics

- [ ] Enter `25.50` in the `≤ $` field with **Any price**. The request sends
  `price_max_cents=2550`.
- [ ] The ceiling result contains free events and paid USD events whose known maximum is at most
  `$25.50`; it contains neither a higher known maximum nor an unlisted, unknown, or non-USD price.
- [ ] Combine **Paid** and `≤ $25.50`; only known paid USD events at or below the ceiling remain.
- [ ] Selecting **Free** or **Unlisted** clears/disables the maximum field. The client never combines
  `price=unknown` with `price_max_cents`.
- [ ] A title, organizer, host, speaker, partner/organization, venue, source, publisher, or provider
  term in the main search narrows the server result before pagination.

### Cards, map, provenance, and safe entity links

- [ ] Selecting the main card summary/title expands it; selecting it again collapses it, and opening
  another card closes the previous one.
- [ ] The event time opens a Google Calendar draft and the venue/address opens Google Maps without
  also toggling the card.
- [ ] The map occupies the majority of the view. Its right rail lists only events in the current map
  bounds, scrolls independently, and includes a retained source/provider label on every preview.
- [ ] Selecting a map marker or preview card focuses the same event; selecting blank map space
  clears the active card.
- [ ] **View event** opens the retained public registration/event URL in a new tab.
- [ ] Organizer, host, speaker, and organization names remain usable filter facets when present.
  An external profile icon appears only for an explicit verified direct profile.
- [ ] A person link, when present, is a direct HTTPS LinkedIn `/in/...` URL. An organization link,
  when present, is a direct HTTPS LinkedIn `/company/...` URL or canonical HTTPS organization site.
  No link opens a LinkedIn search-results page.
- [ ] No card claims to show a named attendee/participant roster. Aggregate attendance and
  registration/waitlist/price facts may appear when the source explicitly provides them.

### Historical browse truthfulness

- [ ] The event rows and source counts for an explicit past range use the same bounded interval.
- [ ] The browser makes no provider request while moving month by month; every result comes from
  `/v1/catalog/events` and the persisted current projection.
- [ ] An empty past month is presented as no retained current-projection events, not as proof that
  the provider had no events then. Events removed from the source's latest successful refresh are
  outside this browse contract even if older canonical records remain.

### Conditional Meetup catalog check

Run this only after the reviewed Meetup source has a successful current projection:

- [ ] Select **Meetup San Francisco** or **Meetup New York** and confirm results come from the
  persisted catalog without a browser-side request to `meetup.com`.
- [ ] A map preview names the Meetup source, and **View event** opens the retained public event URL.
- [ ] Event facts contain only public root Event JSON-LD fields; there is no member, RSVP, named
  attendee, cookie, OAuth, or raw provider payload.
- [ ] A successful zero-event projection is shown as empty/current rather than mislabeled as a
  transport failure.

For refresh setup or a failed Meetup projection, stop this consumer test and follow
[the Meetup ingestion runbook](meetup-ingestion-runbook.md).

## 1. Welcome and onboarding

Open a fresh private/incognito browser window yourself, then paste
`http://127.0.0.1:8000/app` into its address bar. The macOS `open` command does not guarantee a
private window.

- [ ] If it remains visible long enough to observe, the boot message says
  **Preparing your concierge…**.
- [ ] The welcome heading is **One brief. Something worth going to.**
- [ ] Submitting an empty or invalid email is blocked by browser validation.
- [ ] Select two interests and confirm their pressed appearance toggles on and off.
- [ ] Enter the disposable email and select **Meet my concierge**.
- [ ] The product shell appears and the toast says **Your local concierge is ready.** If private
  storage is unavailable, the allowed alternative says the concierge is ready for this tab and
  that private browsing prevented session storage.
- [ ] The service indicator becomes **All systems ready**.
- [ ] Settings shows the same email, **Local demo browser**, and **Local mock cloud**.
- [ ] No unexpected red error appears in the browser Console.

Before doing more work, capture the disposable tenant ID in DevTools Console:

```js
copy(JSON.parse(localStorage.getItem("events-concierge.local-session.v1"))?.tenantId)
```

Paste it into the test record. If that evaluates to `undefined` because private storage is
unavailable, copy `tenant_id` from the preserved `POST /v1/onboard` Network response instead and do
not reload this tab. This UUID is required for the final erasure verification.

## 2. Shell, navigation, hash routes, and reload persistence

- [ ] Select **Concierge**, **Plans**, **To do**, and **Settings**.
- [ ] The URL hashes become `#/concierge`, `#/plans`, `#/tasks`, and `#/settings`.
- [ ] Only the selected page is visible and the selected navigation item is highlighted.
- [ ] **Plans** initially shows either real tenant plans or **No upcoming plans yet**.
- [ ] **To do** initially shows either real tenant tasks or **Nothing needs you right now**.
- [ ] When private-window local storage is available, reload on `#/settings`; the same account and
  Settings page return without onboarding again. Otherwise mark reload persistence `NOT EXERCISED`.
- [ ] Navigate back to Concierge and verify the saved email/profile remains.

View navigation intentionally uses `history.replaceState`, so switching views does not create a
Back/Forward entry for every tab change.

## 3. Read-only preview boundary

Open DevTools **Network**, filter to Fetch/XHR, and enable **Preserve log**.

1. On Concierge, leave the request blank and select **Find matching events**.
2. Enter or choose a broad prompt, for example **Friday live music**.
3. Leave **Show me options** selected and submit.

Expected:

- [ ] Blank submission says **Give me a sentence about what sounds good.**
- [ ] The submit label is **Find matching events** in preview mode.
- [ ] Exactly a read-only `POST /v1/feed` is made; this action does not make
  `POST /v1/requests`.
- [ ] The **Saved and working** progress panel stays hidden.
- [ ] The preview does not add a new item to **Recent briefs**.
- [ ] The result renders at most three cards, or honestly says **No confident matches yet**.
- [ ] Understood timing/category/price details, when present, match the sentence.

An empty result is valid when the current catalog has no confident match. Record the card-specific
checks below as `NOT EXERCISED` in that case. Catalog refresh is not an HTTP side effect and does
not happen because a consumer searches or changes a filter. The separate local cadence scheduler
may refresh reviewed due sources in the background. If you deliberately want to refresh one
reviewed source, run:

```bash
make catalog-refresh SOURCE_KEY=luma-sf
# or:
make catalog-refresh SOURCE_KEY=luma-nyc
# or:
make catalog-refresh SOURCE_KEY=meetup-sf
# or:
make catalog-refresh SOURCE_KEY=meetup-nyc
```

That command changes the persistent catalog and can make outbound requests.

### Pick-card behavior, when cards exist

- [ ] A card shows title, time, venue truth, price truth, source, and rationale when available.
- [ ] **Looks good** becomes **Noted ✓** and remains disabled.
- [ ] **Not for me** removes only that card and moves focus to a useful next control.
- [ ] **Open event site ↗**, when present, opens a new tab rather than replacing the app.
- [ ] **Try more options**, when present, shows the next batch without duplicating the visible batch.
- [ ] A preview-only or conflicted event is labeled honestly rather than shown as registered.

## 4. Durable “Find and handle it” request

Use a new but similar sentence so the request is easy to identify:

```text
Find and handle a free live music event Friday evening after 7
```

1. Select **Find and handle it**.
2. Confirm the submit label changes to **Start durable request**.
3. Submit once. Do not repeatedly click while the button is busy.
4. In DevTools, save the `request_id` from the `POST /v1/requests` response.

Expected:

- [ ] One `POST /v1/requests` is made.
- [ ] The page shows **Saved and working** and **Your concierge has the brief.**
- [ ] The toast begins **Your brief is saved** and does not claim registration is complete.
- [ ] **Recent briefs** includes the exact sentence with an honest request/lifecycle label. A fast
  workflow may already show **Finding a safe route**, **Needs your tap**, or
  **On your calendar** instead of **Saved** or **Started**.
- [ ] When private-window local storage is available, reloading the page keeps the durable brief.
  Otherwise mark this reload check `NOT EXERCISED` and keep using the current tab.
- [ ] While the page remains visible, the UI refreshes a pending brief without overlapping requests.
- [ ] Select **Track my plans →** and confirm it opens Plans.
- [ ] Select **Open to-dos** from Concierge and confirm it opens To do.
- [ ] Select the item under **Recent briefs** and confirm its sentence returns to the request box
  with a **Brief restored** toast.
- [ ] If the workflow resolves during the observation window, it shows a selected lifecycle outcome
  or an honest **No match** result. A still-honest pending state after two minutes is a diagnostic
  threshold, not by itself a UI failure; continue with the checks below.

The deterministic parent workflow ID is:

```text
req:<tenant-id>:<request-id>
```

At [`http://127.0.0.1:8234`](http://127.0.0.1:8234), search for that exact ID. A quickly completed
workflow may be under **Closed** or **All** rather than **Open**.

- [ ] The workflow exists exactly once.
- [ ] Its history advances after the worker starts processing it.
- [ ] A newly created healthy-stack workflow has no `Workflow Task Timed Out` event.

If it does not advance:

```bash
docker compose --profile app ps
docker compose --profile app logs --since=10m api workflow-worker request-starter
curl -sS http://127.0.0.1:8000/readyz
```

Confirm that `make stack`, rather than `make api`, launched the application.

## 5. Plans and withdrawal

Open **Plans** and select **Refresh plans**.

- [ ] The counters distinguish **Confirmed plans**, **In progress**, and **Need your tap**.
- [ ] A selected lifecycle appears once with the expected title and date.
- [ ] When a candidate was selected, its label is honest: for example
  **Finding a safe route**, **Needs your tap**, **On your calendar**, or **Confirmed**.
- [ ] Any conflict, cancellation, or reschedule warning is visible on the card.
- [ ] **Event site**, when present, opens a new tab.

If **Leave event** is available in local mock mode:

1. Select it and cancel the native confirmation first.
2. Confirm the plan did not change.
3. Reopen it and accept only if this is still the disposable local tenant.

- [ ] Cancelling makes no request and leaves the plan unchanged.
- [ ] Accepting shows **Withdrawal accepted. I’ll reconcile the source and calendar.**
- [ ] The state becomes **Leaving event** and eventually converges after refresh.

Do not perform this step after changing the environment to real provider adapters unless the
registration and provider account are genuinely disposable.

## 6. Honest handoff and To-do behavior

This section is conditional: it requires the chosen source to produce a handoff.

Open **To do** and select **Refresh to-dos**.

- [ ] A task shows a human-readable reason such as **Sign-in required** or
  **A quick human check**.
- [ ] Its event, time, venue, expiry, and **Open event site ↗** are clear.
- [ ] Selecting **I finished registration** opens **Did you finish on the event site?**
- [ ] **Not yet** or Escape closes the dialog without changing task state.
- [ ] Keyboard focus returns to the originating task control.

Only after actually completing the disposable source flow:

- [ ] **Yes, verify it** changes the button to disabled **Verification requested**.
- [ ] The toast says verification started; it does not immediately claim a calendar update.
- [ ] Refresh eventually removes or advances the task only after lifecycle verification.

If no handoff is generated, mark this section `NOT EXERCISED`; do not fabricate one with a real
provider.

## 7. Preferences and session behavior

In **Settings**:

- [ ] Toggle at least one interest and select **Save my taste**.
- [ ] The toast says **Your taste signals are saved.**
- [ ] Reload the page and confirm the interest remains selected.
- [ ] The account facts still show the disposable email and local-demo environment.

Optional sign-out check:

1. Save the tenant UUID first.
2. Select **Leave this session**.
3. Confirm the welcome screen returns.

- [ ] The browser's `events-concierge.local-session.v1` key is gone.
- [ ] Server data was not described as erased.

Because local sign-out intentionally loses the browser reference, restore this disposable test
tenant before the final erasure test with DevTools Console:

```js
localStorage.setItem(
  "events-concierge.local-session.v1",
  JSON.stringify({tenantId: "PASTE-TENANT-UUID"})
);
location.assign("/app#/settings");
```

This restoration technique is valid only for `local_demo:true`; it is not a production login path.

## 8. Mobile, keyboard, and reduced-motion pass

Use browser device emulation at approximately `390 × 844`.

- [ ] The desktop rail disappears and the four-item bottom navigation appears.
- [ ] Concierge, Plans, To do, and Settings have no horizontal page overflow.
- [ ] Text and primary controls are usable without pinch zoom.
- [ ] Task and plan actions do not overlap or render off-screen.

Using only the keyboard:

- [ ] Tab focus is visible.
- [ ] Enter activates each navigation control.
- [ ] Navigation moves focus to the main content.
- [ ] Escape closes the task-completion and erasure dialogs.
- [ ] Closing a dialog returns focus to the control that opened it.

With the operating system or DevTools set to **prefers-reduced-motion: reduce**:

- [ ] View changes remain usable and avoid smooth animated scrolling.

## 9. Optional controlled Temporal outage

Run this only when no important workflow is currently active. Keep the browser open.

```bash
docker compose stop temporal
curl -sS -i http://127.0.0.1:8000/readyz
```

Expected: HTTP 200 with database `ready` and Temporal `degraded`.

- [ ] The UI status becomes **Requests safely queued** after reload or a new readiness check.
- [ ] A single new durable request remains honestly saved/queued.
- [ ] It is not presented as registered while Temporal is unavailable.
- [ ] A preview remains a preview and does not become durable.

Bounded connection/retry warnings caused by this intentional outage are expected. They must stop
after recovery. Recover:

```bash
docker compose start temporal
docker compose --profile app ps temporal
curl -sS http://127.0.0.1:8000/readyz
```

Rerun `docker compose --profile app ps temporal` until it says `healthy`, then evaluate the
readiness response. It must return `temporal:"ready"`.

- [ ] The queued request advances once after recovery.
- [ ] There is no duplicate parent workflow.
- [ ] API and workers return to stable running/healthy state.

Do not resubmit the same UI command repeatedly during the outage.

## 10. Destructive account-erasure test — run last

Proceed only when:

- `GET /v1/ui-config` still reports `"local_demo":true`;
- the `account-erasure` service is running;
- the tenant UUID has been recorded; and
- this is a disposable account whose data may be permanently removed.

In a fresh terminal, canonicalize the saved UUID and prove that it resolves to the exact disposable
email before opening the Danger Zone:

```bash
cd /Users/iliazlobin/Claude/events-concierge
EXPECTED_TEST_EMAIL='manual-ui-20260723@example.test'
TENANT_ID=$(
  python3 -c 'import sys, uuid; print(uuid.UUID(sys.argv[1]))' 'PASTE-TENANT-UUID'
) || { echo 'Invalid tenant UUID; stopping.'; exit 1; }
export TENANT_ID EXPECTED_TEST_EMAIL
curl -fsS -H "X-EC-Tenant-ID: $TENANT_ID" http://127.0.0.1:8000/v1/me |
  python3 -c '
import json
import sys

expected = sys.argv[1]
actual = json.load(sys.stdin).get("notify_email")
if actual != expected:
    raise SystemExit(f"STOP: tenant belongs to {actual!r}, not {expected!r}")
print(f"Verified disposable account: {actual}")
' "$EXPECTED_TEST_EMAIL" || { echo 'Tenant/email verification failed; stopping.'; exit 1; }
```

Use the same email during onboarding, or replace `EXPECTED_TEST_EMAIL` with the exact disposable
email from the UI. The command intentionally exits that terminal on an invalid UUID, an API
failure, or an email mismatch. **Do not continue to the erasure dialog unless it prints
`Verified disposable account` with the correct address.**

Add at least one preference and, if available, one harmless durable request before deletion.

### Dialog and acceptance

1. Open **Settings → Erase my account…**.
2. Read the irreversible-action copy.
3. Enter `delete my account`; the destructive button must remain disabled.
4. Enter `DELETE MY ACCOUNT ` with a trailing space; it must remain disabled.
5. Select **Keep my account** and confirm the account remains usable.
6. Reopen the dialog and type exactly `DELETE MY ACCOUNT`.
7. Submit once.

Expected:

- [ ] Focus initially enters the confirmation field.
- [ ] Only the exact phrase enables **Erase account**.
- [ ] One `POST /v1/me/erasure-requests` returns HTTP 202.
- [ ] The response has `Cache-Control: no-store, max-age=0` and normally `Retry-After: 30`.
- [ ] Save the response `request_id` in the test record.
- [ ] The product shell disappears and **Account erasure is underway.** receives focus.
- [ ] The page explicitly says this is an acceptance receipt, not proof every provider is finished.
- [ ] The browser does not poll for completion after losing its authenticated account.

In DevTools Console, both results must be `null`:

```js
localStorage.getItem("events-concierge.local-session.v1")
localStorage.getItem("events-concierge.erasure-request.v1")
```

### Worker and database convergence

Follow the worker for up to 90 seconds, then press `Ctrl-C` to stop following logs:

```bash
docker compose --profile app logs --since=5m -f account-erasure
```

The cycle counters are global batch aggregates, so another pending erasure may make `claimed` or
`completed` greater than one. Require `rescheduled=0`, `lost_leases=0`, and no
`account erasure worker pass failed`; use the tenant-specific receipt below as the authoritative
completion proof.

Inspect the permanent pseudonymous, payload-free command tombstone:

```bash
docker compose exec -T \
  -e MANUAL_TENANT_ID="$TENANT_ID" \
  postgres psql -X -v ON_ERROR_STOP=1 -U ec -d ec -P pager=off <<'SQL'
\getenv tenant MANUAL_TENANT_ID
SELECT request_id,
       status,
       workflow_target_count,
       calendar_target_count,
       external_effects_drained_at IS NOT NULL AS effects_drained,
       workflows_cancelled_at IS NOT NULL AS workflows_cancelled,
       calendar_purged_at IS NOT NULL AS calendar_purged,
       browser_sessions_revoked_at IS NOT NULL AS sessions_revoked,
       credential_vault_purged_at IS NOT NULL AS vault_purged,
       object_store_purged_at IS NOT NULL AS objects_purged,
       completed_at IS NOT NULL AS completed,
       retained_audit_rows,
       attempt_count,
       last_failure_stage,
       lease_token IS NOT NULL AS leased
FROM public.account_erasure_requests
WHERE tenant_id = :'tenant'::uuid;
SQL
```

Expected:

- one row with the same request ID;
- `status=completed`;
- every stage boolean and `completed` is `t`;
- normally `attempt_count=1`;
- `last_failure_stage` is null; and
- `leased=f`.

If status is still `erasing`, wait 30 seconds and rerun the query. A non-null failure stage or a
rescheduled worker cycle is a failure to investigate, not permission to issue a second deletion.

Verify representative tenant-owned rows are gone:

```bash
docker compose exec -T \
  -e MANUAL_TENANT_ID="$TENANT_ID" \
  postgres psql -X -v ON_ERROR_STOP=1 -U ec -d ec -P pager=off <<'SQL'
\getenv tenant MANUAL_TENANT_ID
SELECT relation, rows_left
FROM (
  SELECT 'tenants', count(*) FROM public.tenants WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'event_requests', count(*) FROM public.event_requests WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'lifecycle', count(*) FROM public.lifecycle WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'transition_ledger', count(*) FROM public.transition_ledger WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'handoff_tasks', count(*) FROM public.handoff_tasks WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'outbox', count(*) FROM public.outbox WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'notification_ledger', count(*) FROM public.notification_ledger WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'calendar_bindings', count(*) FROM public.calendar_bindings WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'google_calendar_sync_state', count(*) FROM public.google_calendar_sync_state WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'watch_subscriptions', count(*) FROM public.watch_subscriptions WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'event_change_deliveries', count(*) FROM public.event_change_deliveries WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'event_change_calendar_repairs', count(*) FROM public.event_change_calendar_repairs WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'tenant_ranking_profiles', count(*) FROM public.tenant_ranking_profiles WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'tenant_ranking_feedback_receipts', count(*) FROM public.tenant_ranking_feedback_receipts WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'tenant_source_consents', count(*) FROM public.tenant_source_consents WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'request_start_outbox', count(*) FROM public.request_start_outbox WHERE tenant_id=:'tenant'::uuid
  UNION ALL SELECT 'request_outcome_links', count(*) FROM public.request_outcome_links WHERE tenant_id=:'tenant'::uuid
) AS remaining(relation, rows_left)
WHERE rows_left <> 0;

SELECT count(*) AS retained_rows,
       count(*) FILTER (WHERE consent_ref IS NOT NULL) AS consent_refs_remaining,
       count(*) FILTER (WHERE pii_shredded_at IS NULL) AS unshredded_rows
FROM public.registration_action_audit
WHERE tenant_id = :'tenant'::uuid;
SQL
```

Expected: the first query returns no rows. In the second query, `retained_rows` equals the receipt's
`retained_audit_rows`, while `consent_refs_remaining=0` and `unshredded_rows=0`. The permanent
pseudonymous, payload-free `account_erasure_requests` tombstone intentionally remains.

Finally, the old tenant must be permanently unusable:

```bash
curl -i -H "X-EC-Tenant-ID: $TENANT_ID" http://127.0.0.1:8000/v1/me
```

Expected after completion: HTTP 410 with `account erased`. HTTP 423 with
`account erasure in progress` is valid only while the asynchronous worker is still converging.

### What this erasure test does not prove

Local mock mode cannot prove:

- the production OIDC `prompt=login`, `max_age=0` recent-authentication step-up;
- real Redis-wide session revocation;
- real Calendar, vault, object-store, notification, or source-provider deletion;
- Temporal Cloud physical history or archive/export removal;
- backup expiration, legal holds, or regulatory retention policy; or
- completion within the production erasure deadline.

Those remain staging/production evidence gates.

## 11. Finish and report

Stop log following with `Ctrl-C`. Leaving the stack running requires no action. To stop containers
while preserving local data:

```bash
make down
```

To restart the same persisted state later:

```bash
make stack
```

Summarize the run:

```text
Required checks passed:
Required checks failed:
Conditional checks not exercised:
First failing test section:
Visible error:
Request/tenant/workflow correlation IDs:
Relevant API status:
Relevant log excerpt:
Screenshot filenames:
Reproduces after reload: yes / no
```

Never paste login cookies, CSRF values, OAuth credentials, handoff capability URLs, notification
payloads, or provider tokens into the report.
