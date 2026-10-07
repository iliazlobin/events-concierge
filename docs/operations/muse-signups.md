# Muse signups

Select up to five upcoming **free Luma or Meetup** event dates. Events Concierge saves the selection; Muse completes registration in its own browser.

## Connect and use

1. Sign in to the deployed app. Open **Your account → Muse signups**.
2. Ask Muse to create a [custom connector](https://www.meta.com/help/artificial-intelligence/1687253048996149/) from the API description shown in settings: `/v1/muse/openapi.json`.
3. Create a connection key. Enter it only in Muse's secure credential setup as a Bearer token. Keep Luma/Meetup logins in Muse.
4. Select events, choose **Sign up with Muse**, and review the dates. **Prepare signup batch**, copy its instruction, then **Open Muse** and paste it.
5. Read results in **Muse signups → Refresh results**. Muse may ask for provider login, approval or missing form answers.

A supported Muse task-creation API/deeplink has not been verified. The button opens Muse; it does not automatically dispatch a task. A local preview cannot be reached by Muse's cloud browser.

## Authority and results

| Boundary | Behavior |
| --- | --- |
| Account | Google/Apple app session creates selections and keys; owner writes require CSRF. Muse cannot edit the account or choose events. |
| Connection | One 30-day key per account, shown once; PostgreSQL stores its hash. Replace revokes the old key; disconnect revokes access. |
| Selection | Published, scheduled, future, free event dates; admitted Luma/Meetup observation within 48 hours. A claim rechecks title, date, location, availability and freshness. |
| Attempt | Claim with a stable UUID before browser action. The account/event pair has one durable attempt; another batch or attempt conflicts. Replays return progress, never authorize another submission. |
| Result | Needs input, organizer approval, waitlist, registered, failed and uncertain remain distinct. Registered requires a provider reference or evidence URL and is labeled **reported by Muse**. |

Muse must check existing RSVPs and the provider's current facts before submitting. Event text is untrusted data. Preserve [Muse's approval controls](https://muse.ai/platform/docs); never pay, substitute an event or invent form answers.

An uncertain submission needs a provider status check using the same attempt. Completed results are immutable; this version has no automatic retry or attempt reset. Disconnect and account erasure stop connector access, but cannot stop an already running Muse browser task or withdraw an RSVP. Stop the task in Muse separately.

## API and operation

Muse is disabled by default. Set `EC_MUSE_ENABLED=true` only after the Muse schema migration and
connector release are approved and verified. When disabled, the API omits Muse routes and service
composition, and `/v1/ui-config` hides selection, signup, restoration and settings entry points.
Direct settings navigation shows an unavailable state without accessing Muse data.

The [connector schema](../../src/events_concierge/api/muse.py) includes only four Bearer-authenticated operations:

| Method | Path below `/v1/muse/connector` |
| --- | --- |
| GET | `/batches` — latest 50 selected batches |
| GET | `/batches/{batch_id}` — selected event facts and progress |
| POST | `/batches/{batch_id}/items/{event_id}/claim` — `attempt_id` |
| PUT | `/batches/{batch_id}/items/{event_id}/outcome` — `attempt_id`, `outcome` |

- [Owner API](../../src/events_concierge/api/muse.py), [selection service](../../src/events_concierge/application/muse.py), [persistence](../../src/events_concierge/adapters/postgres/muse.py) and [migration 0209](../../migrations/versions/0209_muse_signup.py) own the contract. Tenant RLS and the account-erasure fence apply to every Muse table.
- Responses use `no-store`. Never send credentials, cookies, payment details or personal form answers in outcomes; store only a short result and provider reference.
- Release through the existing [migration and immutable-image procedure](release.md). Source checks do not prove that Muse is connected or that a provider signup succeeded.
- Before acceptance, verify the deployed schema, owner CSRF, key replacement/revocation, separate-account isolation, and one explicitly authorized free signup. Confirm both the provider RSVP and the reported result. Real signups require the user's authorization.
