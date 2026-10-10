# Muse signups

Queue upcoming **free Luma or Meetup** event dates from the Muse icon on each event card. Events Concierge stores each registration task; Muse completes registration in its own browser.

## Connect and use

1. Sign in to the deployed app. Open **Your account → Muse signups** to configure the connection.
2. Ask Muse to create a [custom connector](https://www.meta.com/help/artificial-intelligence/1687253048996149/) from the API description shown in settings: `/v1/muse/openapi.json`.
3. Create a connection key. Enter it only in Muse's secure credential setup as a Bearer token. Keep Luma/Meetup logins in Muse.
4. Click an event's **Muse icon** to queue its date. Repeat for other events; there are no checkboxes or selection limit across tasks.
5. Open **Registrations**, copy the queue instruction, then **Open Muse** and paste it. Muse may ask for provider login, approval or missing form answers.
6. Follow progress in **Registrations**. The header badge counts unread task updates; opening the panel acknowledges the versions displayed. In-app announcements report new progress and results. Refresh is manual or every 15 seconds while the page is visible; returning to the page refreshes it.

A supported Muse task-creation API/deeplink has not been verified. The button opens Muse; it does not automatically dispatch a task. A local preview cannot be reached by Muse's cloud browser.

## Authority and results

| Boundary | Behavior |
| --- | --- |
| Account | Google/Apple app session creates selections and keys; owner writes require CSRF. Muse cannot edit the account or choose events. |
| Connection | One 30-day key per account, shown once; PostgreSQL stores its hash. Replace revokes the old key; disconnect revokes access. |
| Queue | One durable task per account/event. Repeated clicks return the existing task. Request UUIDs are idempotent and cannot be reused for another date. Published, scheduled, future, free dates require a Luma/Meetup observation within 48 hours. |
| Attempt | Claim with a stable UUID before browser action. A claim rechecks title, date, location, availability and freshness. Competing attempts conflict. Replays return progress, never authorize another submission. |
| Result | Needs input, organizer approval, waitlist, registered, failed and uncertain remain distinct. Registered requires a provider reference or evidence URL and is labeled **reported by Muse**. |
| Notification | Each changed attempt/result advances the task version. Acknowledging an older version leaves a newer update unread. Read state persists per account in PostgreSQL; no email, push permission or browser notification is required. |

Muse must check existing RSVPs and the provider's current facts before submitting. Event text is untrusted data. Preserve [Muse's approval controls](https://muse.ai/platform/docs); never pay, substitute an event or invent form answers.

An uncertain submission needs a provider status check using the same attempt. Completed results are immutable; this version has no automatic retry or attempt reset. Disconnect and account erasure stop connector access, but cannot stop an already running Muse browser task or withdraw an RSVP. Stop the task in Muse separately.

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

- Owner session endpoints: `POST /v1/me/muse/registrations` queues `{request_id, event_id}`; `GET` returns tasks, total/unread counts and `next_cursor`; `POST /registrations/seen` acknowledges `{items: [{event_id, version}]}`. Pages contain up to 100 tasks. Existing batch endpoints remain compatible with saved handoffs.
- [Owner API](../../src/events_concierge/api/muse.py), [queue service](../../src/events_concierge/application/muse.py) and [persistence](../../src/events_concierge/adapters/postgres/muse.py) own the contract. Migrations 0209 and 0214 retain existing attempts and add request aliases, versions and read state. Tenant RLS and the account-erasure fence apply to every Muse table.
- Responses use `no-store`. Never send credentials, cookies, payment details or personal form answers in outcomes; store only a short result and provider reference.
- Release through the existing [migration and immutable-image procedure](release.md). Source checks do not prove that Muse is connected or that a provider signup succeeded.
- Before acceptance, verify the deployed schema, owner CSRF, key replacement/revocation, separate-account isolation, and one explicitly authorized free signup. Confirm both the provider RSVP and the reported result. Real signups require the user's authorization.
