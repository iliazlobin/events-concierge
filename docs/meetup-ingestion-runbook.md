# Meetup public-catalog ingestion runbook

This runbook covers the anonymous, tenant-neutral `meetup_city_jsonld` catalog sources. It does not
authorize or exercise the tenant-scoped Meetup OAuth/RSVP adapter.

## Preconditions

- Migrations `0126` and `0137` are applied (`0137` enables the reviewed detail-request budget).
- The source row is enabled and review-current.
- The local database owner has explicitly admitted `public_jsonld/browser` collection as described
  in [ingestion administration](ingestion-admin.md#local-source-policy-opt-in).
- Outbound HTTPS to the exact reviewed Meetup city URL is permitted.
- The command relay is running for admin-submitted work, or the one-shot catalog command is being
  invoked directly.

The reviewed source keys are:

```text
meetup-sf
meetup-nyc
```

Verify the registry without changing it:

```sql
SELECT source_key,
       display_name,
       seed_url,
       mode,
       enabled,
       handoff_only,
       refresh_interval_minutes,
       min_interval_ms,
       page_limit,
       reviewed_at,
       review_expires_at
FROM catalog_sources
WHERE source_key IN ('meetup-sf', 'meetup-nyc')
ORDER BY source_key;
```

Expected invariants:

- `mode = 'meetup_city_jsonld'`
- `enabled = true`
- `handoff_only = true`
- `refresh_interval_minutes = 120`
- `min_interval_ms = 1500`
- `page_limit = 41` (one city request plus no more than forty detail requests)
- the seed is the exact reviewed San Francisco or New York `/find/` URL
- the sole approved origin is `https://www.meetup.com`

Do not turn a registry edit into a generic Meetup crawler. The adapter independently rejects a
different city URL, extra origin, redirect, page cap, or non-handoff posture.

## Run a refresh

Start the local product stack and inspect the primary operator UI:

```bash
make stack
open http://127.0.0.1:3001/admin
```

The FastAPI-packaged fallback remains available at `http://127.0.0.1:8000/admin`. In either admin,
queue a source refresh and follow its command and run records. HTTP acceptance means the command is
durably queued; it is not proof that provider collection or catalog persistence completed.

For a direct one-shot diagnostic:

```bash
make catalog-refresh SOURCE_KEY=meetup-sf
make catalog-refresh SOURCE_KEY=meetup-nyc
```

For one bounded pass over every source currently due:

```bash
make catalog-cadence
```

These commands can make live outbound requests. They still enforce registry review, policy,
approved origin, pacing, and durable source/run lease checks.

## Verify the result

In the source workspace, verify:

- the latest run reached a terminal state;
- the adapter made one city request and no more than forty deterministically selected, paced detail
  requests; a smaller count is normal when the city snapshot has fewer events;
- candidate, canonical-output, and projected-event counts are plausible;
- current catalog rows retain event URL, schedule, provider/source provenance, and any public
  organizer, location, offer, or availability metadata actually present; and
- no raw HTML, application JSON, token, member, RSVP, or attendee material appears in the admin
  projection.

For enriched candidates, inspect only the bounded provenance vocabulary and counters. A successful
detail identity check may report `enriched` or `validated_no_new_evidence`; an event outside the
forty-detail budget reports `request_cap_not_fetched`. Request failure, redirect refusal, envelope
rejection, missing/malformed root Event JSON-LD, and identity mismatch are per-event best-effort
outcomes: the validated city candidate remains publishable. `detail_applied_field_count` is a field
application count, not a content-quality score, and no status implies attendance or RSVP truth.

A successful run with zero candidates can be truthful when the page contains no future root Event
JSON-LD. It should not be relabeled as a transport or parser failure.

Consumer verification should use the persisted catalog, not a direct provider request:

- filter by the Meetup source in Events, Map, or Calendar;
- confirm each map preview identifies its provider/source;
- open the event URL and verify it targets the retained public Meetup event page; and
- confirm a source selection changes catalog results without triggering a browser-side crawl.

## Failure triage

| Signal | Meaning | Operator response |
|---|---|---|
| `modality_disabled` or blocked admission | Public catalog browser policy is fail-closed | Review sources, then use the documented owner path; do not bypass policy in code |
| Request left reviewed endpoint | Redirect or source identity drift | Contain the source and review the exact URL; do not follow or broaden the origin automatically |
| Response was not HTML or UTF-8 | Upstream contract drift | Preserve run evidence and inspect the public page manually without adding a permissive fallback |
| Response-size limit | Body exceeded the 2 MB bound | Disable/contain if persistent; do not increase the limit without a fresh threat and capacity review |
| No public JSON-LD blocks | The reviewed page no longer exposes the contracted surface | Treat as adapter drift; do not parse `__NEXT_DATA__`, DOM member state, or private APIs as a shortcut |
| Malformed JSON-LD block | Snapshot cannot be normalized atomically | Keep the prior current catalog; fix only after a representative fixture and parser test exist |
| Conflicting event URL | Two public records disagree on stable identity facts | Keep the prior catalog and investigate; never pick one silently |
| `redirect_refused`, `response_too_large`, `invalid_content_type`, or `invalid_utf8` on a detail | One optional detail request violated its envelope | Keep the city candidate; investigate repeated counts without widening the contract |
| `missing_event_jsonld`, `malformed_event_jsonld`, or `identity_mismatch` on a detail | The detail page did not prove the city event's ID, URL, title, and start identity | Keep the city candidate and review provider drift; never enrich from application/member JSON |
| `request_cap_not_fetched` | The city snapshot contained more than forty eligible events | Expected bounded behavior; do not raise the cap without a new capacity and source-contract review |
| Pacer deferred/busy/lease lost | Coordination prevented or displaced collection | Let the durable queue retry or reclaim; do not issue a second uncontrolled request |

Use command history to distinguish acceptance, worker claim, source execution, and catalog commit.
Worker-claim build identity is operational evidence, not proof that that process performed the
provider request.

## Containment and rollback

Pause the exact source through the reviewed admin control or migration-owner procedure; do not
quarantine the tenant OAuth action lane merely because the anonymous city page changed, and do not
clear an existing source quarantine as part of rollback.

Migration `0137` downgrade returns the exact reviewed rows to the one-city-request contract and
advances their revisions again; it does not reuse an older source revision. Migration `0126`
downgrade deliberately disables the two source rows and reclassifies them as generic `public_jsonld`
before removing the closed mode. Neither downgrade deletes collected catalog evidence.
Routine production rollback should prefer compatible code or a forward fix rather than an Alembic
downgrade.

After recovery, run one guarded source refresh, confirm a terminal successful run and plausible
catalog delta, then re-enable normal scheduling.

## Privacy incident guardrail

If named attendee/member data, OAuth material, cookies, or raw application payloads appear anywhere
in the shared catalog or admin output:

1. disable the affected source immediately;
2. preserve sanitized operational evidence without copying the sensitive payload into tickets;
3. treat the event as a visibility-classification/privacy incident;
4. identify and remove the invalid projection through a reviewed repair path; and
5. do not resume until a regression fixture proves the parser reads only root Event JSON-LD plus
   bounded visible text beneath an exact `Details` heading and emits no roster/member data.

The OAuth lane has its own consent, tenant isolation, and erasure obligations. This runbook does not
grant authority to enable it.
