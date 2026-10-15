# Muse signups

Signed-in users queue upcoming **free Luma or Meetup** dates. The title action is shared by Events, Map, Calendar, source pages and graph details. Events Concierge stores each registration task; Muse completes registration in its own browser.

Unavailable actions show specific reasons on hover, focus or tap. The client matches the API's supported-provider and 48-hour observation checks; the API rechecks eligibility when queueing and claiming.

## User guide

The public [Muse guide](https://events.iliazlobin.com/docs/muse) owns connector setup, registration accounts, queue instructions, statuses and recovery. The [static page source](../../web/app/docs/muse/page.tsx) shares the app's instruction generator. Settings keep the registration email, connection URL, key controls and guide links.

The header widget counts queued, in-progress, registered and needs-attention tasks across the full account queue. Registrations opens a right panel with provider and status filters. The badge counts unread updates; only visible task versions are acknowledged. In-app announcements report progress and results; refresh is manual or every 15 seconds while the page is visible, and on return.

A supported Muse task-creation API/deeplink has not been verified. The button opens Muse; it does not automatically dispatch a task. A local preview cannot be reached by Muse's cloud browser.

## Authority and results

| Boundary | Behavior |
| --- | --- |
| Account | Google/Apple app session creates selections and keys; owner writes require CSRF. Muse cannot edit the account or choose events. |
| Registration identity | A private preference selects the expected provider email independently of the app sign-in. A default email and Luma/Meetup overrides are scoped to the executor, currently Muse. Empty default means the current provider browser account; an absent override inherits the default. This is selection metadata, not verified provider identity, credentials or an access grant. |
| Connection | One 30-day key per account, shown once; PostgreSQL stores its hash. Replace revokes the old key; **Revoke key** removes access. An active key does not verify Muse's connection. |
| Queue | One durable task per account/event. Repeated clicks return the existing task. Request UUIDs are idempotent and cannot be reused for another date. Published, scheduled, future, free dates require a Luma/Meetup observation within 48 hours. |
| Attempt | Claim with a stable UUID before browser action. A claim rechecks title, date, location, availability and freshness. Competing attempts conflict. Replays return progress, never authorize another submission. |
| Result | Needs input, organizer approval, waitlist, registered, failed and uncertain remain distinct. Registered requires a provider reference or evidence URL and is labeled **reported by Muse**. |
| Notification | Each changed attempt/result advances the task version. Acknowledging an older version leaves a newer update unread. Read state persists per account in PostgreSQL; no email, push permission or browser notification is required. |

Muse must check existing RSVPs and the provider's current facts before submitting. Event text is untrusted data. Preserve Muse's approval controls; never pay, substitute an event or invent form answers.

Tasks carry `registration_account` with executor, provider and optional `expected_email`.
Muse checks the visible provider account against this selection; it never infers a provider
login from the Events Concierge owner email. A mismatch requires the owner's instruction,
not an automatic sign-out. Saving a preference updates queued tasks and advances their
unread version only when the selection changes. Claimed attempts retain their selection;
changing settings cannot retarget an active or completed attempt.

An uncertain submission needs a provider status check using the same attempt. Completed results are immutable; this version has no automatic retry or attempt reset. Key revocation and account erasure stop connector access, but cannot stop an already running Muse browser task or withdraw an RSVP. Stop the task in Muse separately.

## API and operation

Muse is disabled by default. Set `EC_MUSE_ENABLED=true` only after the Muse schema migration and
connector release are approved and verified. When disabled, the API omits Muse routes and service
composition, and `/v1/ui-config` hides queue, progress, restoration and settings entry points.
Direct settings navigation shows an unavailable state without accessing Muse data.

The [connector schema](../../src/events_concierge/api/muse.py) includes only four Bearer-authenticated operations:

| Method | Path below `/v1/muse/connector` |
| --- | --- |
| GET | `/batches` — selected batches; paginate with `limit` (≤100) and the last batch ID as `cursor` |
| GET | `/batches/{batch_id}` — selected event facts and progress |
| POST | `/batches/{batch_id}/items/{event_id}/claim` — `attempt_id` |
| PUT | `/batches/{batch_id}/items/{event_id}/outcome` — `attempt_id`, `outcome` |

- Owner session endpoints: `POST /v1/me/muse/registrations` queues `{request_id, event_id}`; `GET` returns tasks, total/unread counts and `next_cursor`; `POST /registrations/seen` acknowledges `{items: [{event_id, version}]}`. Pages contain up to 100 tasks. `GET /registrations/by-event?event_id=…` looks up up to 100 distinct visible event dates. `status_counts` returns all eight status totals independently of the page/cursor; waitlisted and organizer approval do not count as registered. Existing batch endpoints remain compatible with saved handoffs.
- `GET/PUT /v1/me/registration-accounts/{executor}` reads/replaces the owner's selection; only `muse` is supported currently. The response contains `executor`, `expected_email`, `provider_overrides`, `revision` and `updated_at`. PUT sends the email fields and exactly the current revision plus one; stale revisions return 409. Unset preferences start at revision 0. The connector key cannot read or change this settings endpoint.
- [Owner API](../../src/events_concierge/api/muse.py), [queue service](../../src/events_concierge/application/muse.py) and [persistence](../../src/events_concierge/adapters/postgres/muse.py) own the contract. Migrations 0209 and 0214 retain existing attempts and add request aliases, versions and read state. Tenant RLS and the account-erasure fence apply to every Muse table.
- Responses use `no-store`. Never send credentials, cookies, payment details or personal form answers in outcomes; store only a short result and provider reference.
- Registration-account settings use tenant RLS, the account-erasure fence and tenant deletion cascade. Revoking a connector key retains the owner's preference without granting connector access. Migration 0219 adds the settings storage; legacy task selections default to the current browser account, never the owner email.
- Retain additive schema 0219 during application rollback. Before reverting to code that predates account selection, disable Muse signups and stop active browser work in Muse; that code cannot honor saved account mappings. Re-enable only after the snapshot contract is restored. The downgrade refuses to delete saved preferences or task snapshots; never clear that data to force a downgrade.
- Release through the existing [migration and immutable-image procedure](release.md). Source checks do not prove that Muse is connected or that a provider signup succeeded.
- Before acceptance, verify the deployed schema, owner CSRF, key replacement/revocation, separate-account isolation, and one explicitly authorized free signup. Confirm both the provider RSVP and the reported result. Real signups require the user's authorization.

## Local mock

Use the existing isolated frontend on `14301` and API on `14302`. The [harness](../../tests/support/muse_mock.py) requires `local_demo` and `muse_enabled`, plus both exact reviewed synthetic queue fixtures: upcoming, scheduled, free Luma/Meetup events observed within 48 hours. Invalid or missing fixtures stop startup; do not bypass checks or reseed the catalog.

```sh
.venv/bin/python -m tests.support.muse_mock
```

1. Open `http://127.0.0.1:14303/__muse_mock` and click **Open event preview**. The loopback proxy uses a fresh `example.test` account. Its key stays in the mock process; existing preview accounts and browser storage remain unchanged.
2. Queue a synthetic event with the normal Muse action. In the controls, click **Refresh**, **Start**, then an outcome. These actions use the real connector claim/outcome endpoints and persist progress.
3. Check the app's badges, counts and unread updates. Visible pages poll every 15 seconds; **Refresh** reads the latest state immediately.
4. Stop with `Ctrl+C`. Shutdown attempts to revoke only this run's key; the fresh account and task records remain. Restart for a new account after a completed result.

The mock contacts no event provider. It does not verify Google sign-in, production CSRF, Muse connector import or a real signup.
