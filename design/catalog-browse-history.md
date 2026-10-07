# Bounded calendar history over retained catalog observations

**Status:** implemented · **Date:** 2026-07-31

<a id="decision"></a>

## Read behavior

`GET /v1/catalog/events` has two date modes:

1. With neither `starts_after` nor `starts_before`, it is a live-catalog browse. Events with a known
   end remain eligible while ongoing; ended events and elapsed events without an end are excluded.
2. With both timezone-aware bounds, an event must **start** in the supplied `[start, end)` range.
   An earlier start is excluded even when the event continues into the range. It may be
   future, mixed, or wholly past and participates in the cursor's filter identity. Unscoped ranges
   are limited to 370 days; a selected source may request up to 7,305 days for its archive view.

All filters execute before the stable `(start_at, canonical_event_id)` keyset page. Source/provider
facets and counts apply the same date predicate as event rows. Reads remain side-effect free: a
historical request never fetches a provider, queues a refresh, or changes source cadence.

- Calendar months use `[local month start, next local month start)`; navigation preserves filters and clears the cursor.
- Source changes in the filter bar preserve explicit city/area selections. Incompatible combinations may return no events; edit the chips or Reset to broaden the selection.

- An event card's source shortcut opens that source's history and clears query, custom dates and places.

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
exception is a selected-source range over 370 days: those archives' provider queries remain
scoped to the selected sources to preserve the all-source range limit.

## API contract

- Both bounds or neither; a single bound is rejected.
- Bounds must include timezones and `starts_before > starts_after`.
- All-source ranges span at most 370 days; selected-source archive ranges span at most 7,305 days.
- `starts_before` is exclusive.
- Omitting both bounds means upcoming or ongoing, not “all time.”
- Relative presets such as This week start at the current moment and end at the next local period
  boundary. Custom ranges include the selected calendar days, ending at the following local midnight.
- A paginated client freezes the relative window when it loads the first page and reuses those exact
  bounds for continuations. A new filter scope or refresh starts a new window and clears the cursor.
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

## Recommendation lifecycle and distinct choices (2026-09-08)

Personalized retrieval now uses event-interval overlap: an omitted window excludes ended and
cancelled events while allowing ongoing events with a known end. An explicit window admits
retained history. Registry-owned records use the recommendation observation capability, retaining
interval overlap independently of browse's explicit start-time filters;
request-discovered records without registry observations still use the canonical retrieval path.
Rolloff removes current recommendation eligibility, not the canonical record or its status.
Historical and ongoing feed items carry no registration lane; cancellation records remain retained.
No physical archival, deletion or storage migration is introduced.

Consumer cards label past/ongoing events and source observation freshness. A seven-day threshold
labels an old observation; this is a UI heuristic, not independent confirmation, a source-cadence
SLA, or a claim that recent events are verified. Missing observation dates remain unknown.
Malformed end-before-start intervals are excluded from personalized recommendations.

Session grouping is presentation-only and requires matching title, named organizer, venue, city,
source identity/hosts, description and price state. Each session retains its ID and its own date and
registration URL. These are conservative matching sessions, not an inferred authoritative recurrence
rule. List/chat views disclose “more dates in these results”; calendar/map views retain individual
occurrences. Groups cover the fetched candidate window, not every future session at the publisher.

Personalized broad requests diversify within a five-position relevance neighborhood after scoring;
explicit category or hard-filter requests bypass diversity penalties. Small bounded quality
tie-breaks consider observed freshness, known price/location/end time/topics, proximity when a
request supplies coordinates, and timing. They never fill missing metadata or establish verification. Library/family requests are parsed
as focused categories. The feed uses a fixed 400-candidate budget across offset pages so expanding
an offset does not itself change grouping. This is not a database snapshot: changes in source data
or personalization between requests can still move results. First-20 distinct-choice and named
organizer counts are exposed in feed signals; they are diagnostic counts, not relevance judgments.

Chronological catalog sort remains authoritative by default. Broad catalog list views additionally
provide an opt-in “Vary organizers and activities in loaded results” control. This diversifies only
the loaded, already-filtered pool, explicitly reports that scope, and bypasses focused query/topic/
source and historical views. Choosing a date sort returns to date order. It is a local presentation
preference rather than a new server sort or stored filter; existing URLs and keyset cursors retain
their contracts. No source/category quota or blanket library penalty is applied.
