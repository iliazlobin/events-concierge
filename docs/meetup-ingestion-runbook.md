# Meetup public-catalog ingestion

Operate the anonymous `meetup_city_jsonld` sources. Tenant OAuth/RSVP remains a separate, gated capability. [Source contract](../design/meetup-ingestion.md).

## Preconditions

- Migrations `0126` and `0137` installed; sources enabled and review-current; collection policy admitted.
- Exact source keys: `meetup-sf`, `meetup-nyc`.
- Exact reviewed city URL, sole origin `https://www.meetup.com`, `handoff_only=true`, `page_limit=41`.
- Read current cadence/pacing from the registry; seed defaults are not proof of runtime configuration.
- Admin-submitted work requires the ingestion-command worker and the applicable catalog executor.
- [Local source-policy opt-in](ingestion-admin.md#local-source-policy-opt-in) requires owner review; it must not clear a quarantine.

Read-only registry check:

```sql
SELECT source_key, seed_url, mode, enabled, handoff_only,
       refresh_interval_minutes, min_interval_ms, page_limit,
       reviewed_at, review_expires_at
FROM catalog_sources
WHERE source_key IN ('meetup-sf', 'meetup-nyc')
ORDER BY source_key;
```

The [adapter](../src/events_concierge/adapters/meetup_city/source.py) rejects unreviewed profiles, redirects, extra origins and widened request budgets.

## Run a refresh

1. Open the authorized local admin at `http://127.0.0.1:3001/admin`, or the approved hosted operator surface.
2. Select the exact source and **Queue refresh**.
3. Follow **Track command**, then its source run. Acceptance means queued, not published.

On an authorized local stack, these diagnostics start dependencies, apply migrations and may read Meetup:

```bash
make catalog-refresh SOURCE_KEY=meetup-sf
make catalog-refresh SOURCE_KEY=meetup-nyc
```

`make catalog-cadence` dispatches a bounded pass over **all** due reviewed sources. It is not a Meetup-only check. [Execution and access](ingestion-admin.md#execution-model).

## Verify the result

- Require a successful terminal run and plausible candidate, canonical-output and published counts.
- Each run permits one city request plus at most forty deterministic, paced, identity-checked detail requests.
- Preserve schedule, event URL and observed public organizer/location/price/availability facts with provider/source provenance.
- `enriched` and `validated_no_new_evidence` establish detail validation; neither proves registration or attendance.
- `request_cap_not_fetched` is expected beyond the detail budget; field-application counts are not quality scores.
- Failed optional details preserve the validated city candidate. City-page failure preserves the prior catalog atomically.
- A valid city snapshot with no eligible events can succeed with zero candidates.
- Filter persisted Events/Map/Calendar by source; verify provider labels and retained public event links. Browsing must not trigger collection.
- Worker-claim identity proves a claim, not necessarily the later fetching process.

## Failure triage

| Signal | Response |
| --- | --- |
| `modality_disabled` / blocked admission | Inspect policy and review state; use the owner-controlled path. |
| City redirect, envelope/size failure, missing/malformed JSON-LD or conflicting identity | Preserve last-good catalog; investigate adapter drift without broader scraping. |
| Detail redirect/envelope/JSON-LD/identity failure | Preserve city candidate; inspect repeated bounded failure counts. |
| `request_cap_not_fetched` | Expected cap; expansion needs renewed source/capacity review. |
| Pacer defer/busy or lease loss | Allow guarded retry/reclaim; never issue an uncontrolled duplicate. |

- Limits: 20-second transport timeout, 2 MB UTF-8 HTML, no redirects.
- Read root Event JSON-LD; identity-matched detail enrichment also permits bounded `Details` text and the dedicated visible `Hosted by` assertion.
- Never fall back to application JSON, member state or private APIs.

## Containment and rollback

1. Pause the exact public source through authorized controls; preserve existing quarantines and the separate OAuth lane.
2. Retain sanitized command/run evidence; fix against a representative regression fixture.
3. Prefer compatible code or a forward fix; routine rollback is not an Alembic downgrade.
4. After approved recovery, perform one guarded refresh and verify publication before normal scheduling resumes.

- Migration `0137` downgrade restores the one-city contract with new source revisions; `0126` disables/reclassifies the rows.
- Neither deletes catalog evidence. [Migration/recovery controls](operations/release.md#rollback-and-migration-safety).

## Privacy incident guardrail

- Named attendee/member data, OAuth material, cookies and raw application payloads must never enter shared catalog/admin output.
- If detected: disable the source, preserve sanitized evidence, and classify the visibility/privacy incident.
- Remove invalid projections through reviewed repair; keep sensitive payloads out of tickets.
- Resume only after regression coverage proves the bounded public Event/Details/host contract with no attendee/member roster data.
- This runbook grants no authority to enable tenant OAuth, registration or attendee ingestion.
