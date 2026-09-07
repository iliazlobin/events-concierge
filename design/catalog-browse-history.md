# Bounded calendar history over retained catalog observations

**Status:** implemented · **Date:** 2026-07-31

## Decision

`GET /v1/catalog/events` has two date modes:

1. With neither `starts_after` nor `starts_before`, it is a live-catalog browse. PostgreSQL uses the
   current statement time as the inclusive lower bound, so elapsed events are excluded.
2. With both timezone-aware bounds, the supplied end-exclusive interval is authoritative. It may be
   future, mixed, or wholly past and participates in the cursor's filter identity. Unscoped ranges
   are limited to 370 days; a selected source may request up to 7,305 days for its archive view.

All filters execute before the stable `(start_at, canonical_event_id)` keyset page. Source/provider
facets and counts apply the same date predicate as event rows. Reads remain side-effect free: a
historical request never fetches a provider, queues a refresh, or changes source cadence.

Calendar navigation expresses a month as `[local month start, next local month start)`. Moving
between months retains the selected source and starts with no cursor. Changing source retains place
selections that can still match that source and removes incompatible cities/named areas before the
next request; the source choice must not appear empty solely because a stale geographic constraint
survived it.

## Projection and retention boundary

This is **retained last-known history**, not an audit archive or version store. An event is eligible
only when:

- its source is enabled, reviewed, unexpired, handoff-only, and not a fixture;
- its retained observation was written by a successful non-fixture refresh;
- current and future event portions belong to the source's exact latest successful non-fixture
  refresh; explicit past portions may instead use the retained observation after it rolls off the
  latest crawl;
- the canonical event is not cancelled; and
- the event timestamp and remaining filters match the request.

`catalog_event_observations` upserts one row per source event identity. It preserves first/last seen
metadata and the last successful run that observed the identity, but not each historical payload.
The linked canonical event is also mutable. A past month can therefore answer “which admitted event
identities remain retained with latest-known canonical state in this interval?” It cannot answer
“what exact title, time, location, status, or source payload did we know at the end of that month?”
Reschedules, merges, cancellations, and metadata enrichment are reflected as their latest known
canonical state.

If product requirements later call for exact as-of browsing, add a separate temporal observation
model with valid-time intervals, explicit retention/deletion policy, rebuild semantics, and a
distinct API contract. Do not describe this retained projection as event version history.

Provider facets normally return every currently admitted non-fixture source, including a zero
count, for the same date interval. The repository asks for the global facet list even when event
rows are source-filtered so clients can replace rather than merge source options. The one bounded
exception is a selected-source range over 370 days: that archive's provider query remains
source-scoped to preserve the all-source range limit.

## API contract

- Both bounds or neither; a single bound is rejected.
- Bounds must include timezones and `starts_before > starts_after`.
- All-source ranges span at most 370 days; selected-source archive ranges span at most 7,305 days.
- `starts_before` is exclusive.
- Omitting both bounds means future-only, not “all time.”
- The cursor is valid only for the exact source, date, query, place, and price filter scope that
  created it.
- An explicit previous month may legitimately be empty because the identity was never retained, is
  no longer admitted, is now cancelled, or its latest canonical state moved outside the range.

## Verification

The integration contract proves the split lane with a past identity and a future identity that both
roll off the latest crawl: an explicit mixed window retains only the past identity, while the future
identity remains excluded and the latest successful future event remains visible. It also covers a
wide selected-source archive, rejection of the same unscoped range, zero-count admitted provider
facets, keyset behavior, fixture exclusion, least-privilege grants, and additive capability names.
