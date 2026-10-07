# Entity catalog contract

Published people, organizations, event mentions and stored public profile facts. [Runtime scope](../PROJECT.md#current-milestone-private-discovery-candidate) selects read-only entity browsing in discovery; [Notion design](https://app.notion.com/p/391d865005a88164a182eabc18fe068f) owns architecture and planned enrichment.

## Identity and provenance

- Project organizer, host, speaker and partner roles from admitted catalog observations.
- A verified direct profile URL establishes cross-source identity. Without a stable URL/source ID, keep the name scoped to its asserting source; never merge or enrich by name.
- Each mention retains event, source, role, run and observed name. Publication updates events and the source's entity projection atomically.
- Imported profile links are separate from fetched facts. Facts retain exact provider identity, source URL and observation time; discard raw responses.
- Graph edges show event-role evidence. Co-appearance proves neither attendance nor a social relationship. Exclude named attendee rosters.

## Storage and reads

| Projection | Responsibility |
| --- | --- |
| `catalog_entities` | Indexed entity identity/search |
| `catalog_entity_event_mentions` | Source-attributed event relationships |
| `catalog_entity_source_links` | Exact identity crosswalk to enrichment controls |
| `catalog_entity_external_sources` | Bounded refresh state per exact provider identity |
| `catalog_entity_external_facts` | Typed, searchable, attributed facts |

[API implementation](../src/events_concierge/api/app.py) exposes bounded entity search/detail, an entity directory, overview/selected-entity graphs and exact event-role-name resolution. The resolver cannot open an arbitrary name. Graph responses retain counts and truncation; representative shared events disclose their total.

- `GET /v1/catalog/entities` and `/{entity_id}`: indexed search and detail.
- `GET /v1/catalog/entity-directory`, `/v1/catalog/entity-overview-graph` and `/v1/catalog/entities/{entity_id}/graph`: directory and bounded graph bundles.
- `GET /v1/catalog/entity-resolution`: exact event, role and displayed name.
- `POST /v1/catalog/entities/{entity_id}/refresh`: full profile only, with the existing auth/CSRF contract; absent from discovery. Manual refresh is unleased and must not be autoscaled.
- Browsing reads PostgreSQL; it never refreshes providers. [Release allowlist](../src/events_concierge/api/release_profile.py).
- At widths up to 1100px, graph details follow the canvas in the page scroll. Selecting a node brings its details into view; the return control brings the canvas back. Larger screens show details beside the graph. Fit leaves space for zoom controls.

## Window and insights

Migration `0152` unions current live observations with a trailing 365-day history window, within the query's 370-day ceiling. The branches are disjoint; quality gates run after rebuilds. Entity events list upcoming ascending before past descending.

- Insights use admitted mentions/canonical events, not enrichment calls. Name-only entities retain catalog-derived evidence.
- Counts, topics, places, source coverage and co-appearances describe stored observations, not complete provider coverage.
- Cadence uses a trailing 365-day/leading 90-day window; one or fewer events yields no rate.
- History is retained latest-known state, not an as-of archive. [Browse contract](catalog-browse-history.md).

## Provider boundary

| Adapter | Permitted input/output |
| --- | --- |
| Official website | Exact verified URL; bounded structured metadata/same-as links; SSRF-fenced |
| Wikidata | Exact linked item; public item facts; no name-search identity inference |
| GitHub | Exact linked organization; public organization metadata |
| LinkedIn | Direct profile link only; no scraping or generated search link |
| Licensed provider | Reviewed exact-identity input and typed observations; scoped credentials, terms and operator approval |

Full-profile public refresh replaces only that source's fact projection and updates the search index. Discovery displays stored facts without invoking these adapters. Licensed observations use the separate [enrichment control plane](entity-enrichment-control-plane.md); workers cannot approve their own evidence or replace a direct source identity.

[Entity model](../src/events_concierge/domain/catalog_entities.py), [release browser checks](../tests/e2e/test_next_release_profile.py) and [acceptance checklist](../docs/manual-test-plan.md#discovery-checks) define verification.
