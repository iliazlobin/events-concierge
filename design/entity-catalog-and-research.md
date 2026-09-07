# Entity catalog and public data pipeline

Status: implemented catalog projection over live *and retained* observations, exact public-source
refresh, typed fact indexing, catalog-derived insights, and consumer display. Credentialed/licensed
providers remain operator-gated.

## Product contract

Events establish entity mentions, not identity by themselves. The entity catalog projects public
`organizer`, `host`, `speaker`, and `partner` roles from admitted current catalog observations.

- A verified direct profile URL is a cross-source identity.
- A name without a stable URL or source entity ID stays scoped to the source that asserted it.
- Name-only records are searchable and useful, but they are never automatically merged or sent to
  an enrichment provider.
- Every entity detail keeps the event, source, role, run, and observed name that connect it.
- An ingestion refresh updates event publication and this entity projection in one transaction.

This makes a host such as `PALLADIUM Magazine` browseable immediately while keeping it distinct
from the verified organizer identity `PALLADIUM Magazine Events` until exact evidence connects
them.

## Storage and serving

`catalog_entities` is the indexed consumer projection. `catalog_entity_event_mentions` is the
event relationship, and `catalog_entity_source_links` joins verified catalog identities to the
provider-neutral enrichment control plane. `catalog_entity_external_sources` stores one bounded
refresh state per exact provider identity. `catalog_entity_external_facts` stores normalized,
searchable values with their source URL and observation time; raw responses are discarded.

The public API exposes:

- `GET /v1/catalog/entities` for bounded indexed search;
- `GET /v1/catalog/entities/{entity_id}` for typed public facts, connected sources, events, and
  catalog-derived insights;
- `GET /v1/catalog/entity-resolution` for an exact event + role + displayed-name click.
- `POST /v1/catalog/entities/{entity_id}/refresh` for a bounded exact-source refresh.

The resolver prevents a client from using an arbitrary name to open or infer an entity.

## Projection window (revision 0152)

The projection originally read `fn_list_retained_catalog_browse_observations_v1(source, NULL, NULL)`.
That unbounded mode takes `statement_timestamp()` as its lower bound, so an elapsed event was never
projected and every host or organizer chip on a past event resolved to nothing — 0 of 321 past
events carrying role names had a mention row, against 83% of future ones. Because the consumer can
browse a historical date range, that gap was reachable from the normal product surface.

`fn_refresh_catalog_entity_index_v2` keeps the live call unchanged and unions one bounded history
call covering the trailing 365 days. The two branches are mutually exclusive by the retained
capability's own predicates: live mode requires an event that has not ended plus an observation
from the source's latest successful run, while explicit-window mode admits only an already-ended
event. 365 days keeps an unscoped full rebuild inside the capability's 370-day ceiling, which is
why the window is not widened further. The 0148/0149 quality gate runs after any rebuild, exactly
as the refresh-commit and paged-promotion paths sequence it.

`fn_list_catalog_entity_events_v2` returns `is_past` and orders upcoming ascending before past
descending, so history cannot crowd upcoming appearances out of the caller's limit.

## Catalog-derived insights (revision 0152)

`fn_get_catalog_entity_insights_v1` answers "what does this entity actually do" from evidence we
already hold: appearance counts, first/last event, cadence, typical attendance, free-versus-paid
mix, recurring topics, venues, cities, asserting sources, and recurring co-appearing entities.

It reads admitted mentions and canonical events only. It touches no enrichment table, sends no
provider request, and takes no `identity_status` precondition — so a name-only `source_scoped`
entity, which this design deliberately never researches through a provider, still gets substantive
public evidence. That closes the coverage asymmetry between verified and name-only entities
without weakening the identity rule.

Cadence is measured over a trailing 365-day plus leading 90-day activity window rather than the
whole observed span. The catalog holds provider start dates decades out, and a single far-future
outlier would otherwise flatten the rate toward zero. When that window holds one event or fewer,
no rate is claimed at all.

## Provider strategy

Credential-free public adapters run only from an exact URL already attached to the entity or
published by another exact source. They do not search or merge by display name. Raw provider
payloads are not retained. Credentialed providers still require configuration, terms approval,
and operator approval through the enrichment control plane.

| Adapter | Identity sent | Useful fields | Auto-materialization |
| --- | --- | --- | --- |
| Official website metadata | Exact verified organization URL | canonical URL, description, location, type, founding date, focus, industry, same-as links | Automatic, SSRF-fenced, structured metadata only |
| Wikidata public | Exact Wikidata item URL from a verified source | description, inception, official website, GitHub profile | Automatic exact-item read; no search candidates |
| GitHub public organization | Exact GitHub organization URL from a verified source | description, website, location, creation date, public repositories, followers | Automatic exact-organization read |
| LinkedIn profile | Exact direct person/company URL | profile link | Connected-only; structured fields require a licensed source |
| Credentialed enrichment provider | Exact website or direct company profile | provider-specific company/person fields | Operator-approved typed observations; no name-only requests |

Official documentation supports these boundaries:

- [People Data Labs company enrichment](https://docs.peopledatalabs.com/docs/reference-company-enrichment-api)
  accepts website/profile identifiers and documents match/billing behavior. Its own input guidance
  says website/profile inputs are more unique than name-only requests.
- [Wikibase REST API](https://doc.wikimedia.org/Wikibase/master/js/rest-api/) and
  [Wikidata data access](https://www.wikidata.org/wiki/Help:Data_access) provide public item search
  and entity retrieval. Search is candidate discovery, not identity proof.
- [GitHub organization REST API](https://docs.github.com/en/rest/orgs/orgs?apiVersion=2022-11-28)
  provides public organization metadata for an exact organization login.
- [GLEIF API service](https://www.gleif.org/en/lei-data/gleif-api/access-the-gleif-api) is useful for
  exact legal-entity/LEI data but will not cover many early-stage startups.
- [OpenCorporates API reference](https://api.opencorporates.com/documentation/API-Reference)
  provides company data but loose name search must remain a review candidate and usage is plan/
  license constrained.

LinkedIn pages are never scraped. A direct LinkedIn URL may establish source identity, while any
provider access to LinkedIn-derived data must remain licensed and separately approved.

## Execution stages

1. Extract exact source role facts during Luma/Meetup/other event normalization.
2. Publish the event and refresh its source slice of the entity index atomically.
3. Admit only stable direct URLs to automatic public-source refresh; keep name-only entities
   source-scoped.
4. Read the exact official website, GitHub organization, or Wikidata item through bounded network
   adapters. Connect LinkedIn URLs without scraping them.
5. Replace that source's typed fact projection, retain its source URL and freshness, discard the
   raw response, and update the entity search index.
6. Serve the structured profile, connected-source states, and related events from the entity API.
7. Route any future licensed provider output through the separate reviewed enrichment plane.

This sequence supports later provider workers without weakening identity, source attribution,
rate-limit, or human-review boundaries.
