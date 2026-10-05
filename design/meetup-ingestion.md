# Meetup collection contract

Anonymous public collection and tenant OAuth are separate trust domains. [Operations](../docs/meetup-ingestion-runbook.md) covers city refreshes; [group-calendar operations](../deploy/development.md#public-collection) covers official exports.

| Surface | Input / budget | Authority |
| --- | --- | --- |
| City JSON-LD | Exact reviewed city URL; one listing + at most 40 event details | Shared public catalog, anonymous, handoff-only |
| Group ICS | Exact reviewed `/GROUP/events/ical/`; one feed + at most 100 event details | Shared public catalog, anonymous, handoff-only |
| GraphQL OAuth | Tenant membership/RSVP access | Tenant token and consent; registration remains G2-gated |

OAuth-derived facts never enter shared catalog tables. Public adapters receive no OAuth tokens or cookies and perform no RSVP.

## City mode

Migrations `0126`/`0137` define `meetup_city_jsonld`, `meetup-sf` and `meetup-nyc`. The exact reviewed profile, sole origin `https://www.meetup.com`, handoff posture and 41-unit cap must match. Registry values own active cadence; migration defaults are not runtime measurements.

1. Read the exact city page; extract root/list/`@graph` schema.org Event objects.
2. Validate public event/group URL shapes; exclude past, cancelled and postponed events. Duplicate URLs must agree on title/start; sort deterministically before selecting details.
3. Fetch at most 40 canonical same-origin event pages asserted by the listing. Detail identity must repeat event ID/URL, normalized title and UTC start.
4. Normalize/deduplicate and publish the source snapshot atomically behind its lease.

- Timeout: 20 seconds; UTF-8 `text/html` cap: 2 MB; no redirects or application-state traversal.
- City transport/envelope/identity failure preserves the previous catalog. Optional detail failure preserves the validated city candidate with a closed status; beyond-cap events retain `request_cap_not_fetched`.
- Structured Event facts win for location, price and registration. Bounded visible Details may add descriptions and conservative US addresses or exact free/USD `Cost:`/`Admission:` facts; never override structured evidence.
- Visible Details limits: 64 `p`/`li` nodes, 2,000 characters/node, 12,000 description characters. Attend controls do not prove registration-open.
- Store safe scalar provenance, not HTML/application payloads. Provider classification and source key remain distinct.

## Group mode

Migration `0199` defines `meetup_group_ics`. [Adapter](../src/events_concierge/adapters/meetup_group/source.py): exact same-origin feed, 101 request units, 2 MB, 20-second timeout and minimum 1.5-second pacing; maximum collection horizon 90 days.

- Accept explicitly PUBLIC, dated occurrences with valid Meetup UID/URL identity. Do not expand recurrence rules or collect private/member/RSVP feeds.
- Identity-check detail pages before enrichment. Invalid/incomplete feed, denial or throttling cannot replace the last successful catalog; respect backoff.
- New groups begin disabled/unreviewed; activation uses the audited source-configuration API. Public exports do not establish complete group/city/platform coverage.
- Downgrade to `0195` is blocked while group-source history remains; preserve history with compatible-code recovery.

## Privacy and scheduling

- Exclude attendee/member rosters, member profiles/contact data, credentials, application state and arbitrary links. Public organizer/host assertions are event metadata, not attendance.
- Registry/review/policy admission → Redis pacing → renewable source lease → bounded collection → atomic publication. Lost ownership forbids publishing; failed runs retain the last successful projection.
- Five-minute local cadence or hosted CronJob only queues due work. The ingestion-command worker selects direct/Temporal execution; scheduling success is not publication.
- Stage metrics measure adapter wall time and catalog commit boundaries, not separate extraction quality or continuous resource peaks.

## Tenant OAuth

The existing GraphQL adapter reads membership/RSVP state before `createEventRsvp`, omits `member_id`, refuses browser RSVP and routes ambiguous/ineligible cases to handoff. Default discovery keeps it disabled.

Live schema, authorization, quota scope, autonomous join behavior and retry semantics require the G2 field gate with approved Meetup Pro access. Fixture discovery is not a public catalog source. [Deferred launch gates](../docs/production-operations.md#external-launch-gates).
