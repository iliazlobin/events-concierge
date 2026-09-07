# Catalog event semantics

The consumer catalog exposes a small, provider-neutral topic taxonomy so an event can be scanned
and filtered without parsing arbitrary provider prose in the browser. Topics are canonical event
facts, not personalized classifications.

## Stable taxonomy and extraction

Revision `0136`, extended by `0144`, persists the following slugs on
`canonical_events.topics`:

`ai`, `arts`, `board-games`, `chess`, `community`, `education`, `family`, `food-drink`,
`founders`, `gaming`, `government`, `music`, `networking`, `outdoors`, `sports`,
`technology`, `volleyball`, `wellness`, and `workshop`.

Extraction is deterministic and recomputable. A topic requires either:

- an explicit, bounded provider category/tag/topic/keyword; or
- a bounded phrase in the public event title or description.

Specific terms can emit both a specific facet and a useful parent facet. For example, `beach
volleyball` emits `volleyball`, `sports`, and—only when the outdoor phrase is present—`outdoors`.
Gaming recognizes explicit phrases such as `video game`, `indie game`, `fighting game`, `esports`,
`Smash`, and `Melee`; it deliberately does not match the noisy bare word `game`.
Chess and `bughouse` emit both the specific `chess` topic and the broader `board-games` topic.
Other unambiguous tabletop terms such as `mahjong`, `cribbage`, and `backgammon` emit
`board-games` without being mislabeled as video gaming.

`extraction_evidence` records the field, value, source (`provider_metadata`, `title`, or
`description`), and stable rule name. The taxonomy never infers demographic, political,
religious, health, or other sensitive traits.

## Price and registration boundaries

Structured provider pricing always wins. When it is unknown, the normalization layer may infer
`free` only from unambiguous phrases such as `COST: FREE!`, `free admission`, `admission is free`,
`no cost`, or `free of charge`. Phrases such as `free parking` and `feel free` do not qualify.

Backfill changes a source-link price only when that canonical has exactly one source link, so
canonical text cannot be misattributed to a different publisher. Canonical price remains the
conservative aggregation of current source-link facts. Text never implies registration-open;
registration state requires structured provider evidence.

## Browse and facets

`GET /v1/catalog/events` accepts repeated `topic` parameters. Multiple topics have AND semantics:
an event must contain every selected topic. The selected topics are bound into the keyset cursor
scope, and filtering happens before page selection. The response includes `topic_facets` counts
for all non-topic filters, allowing the client to keep useful additive choices visible even after
a topic is selected.

The consumer uses the server topics directly. Expanded event cards show compact, clickable chips;
clicking adds a topic to the active filter. A stable-width multi-select control supports toggling
and clearing selected topics without shifting the filter rail.

## Meetup detail retention

The reviewed Meetup city adapter already performs a bounded same-origin detail pass: at most forty
identity-matched event pages per city snapshot, no redirects, no application-state/member payloads,
and strict response-size and pacing limits. It retains the longer trusted JSON-LD description, a
bounded visible `Details` section, short explicit JSON-LD keywords, and the event header's dedicated
visible `Hosted by` assertion. The organizing group remains `organizer_name`; named people are
stored separately in `host_names`. Attendee cards, attendee rosters, and RSVP data remain out of
scope. Detail failures preserve the city snapshot with a closed status code; they do not widen
browsing or fetch arbitrary pages.
