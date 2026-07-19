# Event-Source & API Landscape — The Data-Access Tier List (2026)

> Determines, per source, whether the Events Concierge can discover and register through a clean API or must fall to the browser tier — the single biggest driver of adapter design, legal exposure, and per-tenant credential modeling.

## Verified findings

### API-tier: clean-search endpoints

**Ticketmaster Discovery API v2 is the primary high-volume search endpoint and also surfaces Universe inventory, making it the API-tier backbone for ticketed/live events. [confirmed]**
`GET https://app.ticketmaster.com/discovery/v2/events.json?apikey={key}` supports rich keyword / geo (`postalCode`, `radius`, `latlong`, city/state/country) / date / classification (segment, genre, sub-genre) search. Default quota is 5000 calls/day at 5 req/sec; overage returns 429. Deep paging is hard-capped at the 1000th item (`size * page < 1000`) regardless of total result count. Responses carry price ranges, venue geo, dates, and images. Universe (Ticketmaster/Live Nation-owned since 2015) inventory is filterable in the same endpoint via `source=universe` (alongside `ticketmaster`, `frontgate`, `tmr`), so one Discovery integration covers both. Rate limits are raiseable case-by-case subject to ToS/branding review. Note: the exact rate-limit header names (`Rate-Limit`, `Rate-Limit-Available`, `Rate-Limit-Reset`) are commonly reported but were not pinned to a verbatim primary-source line.

**SeatGeek Platform API exists (OAuth client, `/events` search) but has trended toward partner-gating — treat as a conditional API-tier source pending approval. (medium confidence)**
The developer platform (`seatgeek.com/build`, terms at `seatgeek.com/api-terms`) historically keyed `/events` search by `client_id`, but self-serve access is ambiguous as of 2025. Data is strong for US sports/concerts. Design the SeatGeek adapter behind a feature flag contingent on partner credentials; fall back to Ticketmaster/browser.

**Music-tier APIs (Bandsintown, Songkick, Dice.fm) are read-only or partner-gated; none support attendee-side RSVP/purchase via API. (high confidence)**
Bandsintown is read-only (`GET https://rest.bandsintown.com/artists/{artist}/events?app_id=YOUR_APP_ID`), artist/events/venues only, requiring written consent and an assigned `app_id` under a partnership program. Songkick offers a partner Concerts & Festivals API for live-music discovery/tracking. Dice.fm has no public discovery API — only a partner-scoped organizer "Ticket Holders" GraphQL at `partners-endpoint.dice.fm`; attendee discovery/purchase requires the browser tier. All three provide discovery signal but zero autonomous-registration surface.

### Management-only or Pro-gated API (browser tier for the attendee-facing flow)

**Eventbrite killed its public cross-platform event SEARCH; only ID/venue/org lookups remain, forcing the browser tier for Eventbrite discovery. [confirmed]**
Public `GET /v3/events/search/` was removed Dec 12 2019, with a whitelist grace period (10 req/min) ending Feb 20 2020, and has not returned as of mid-2026. Surviving v3 endpoints are lookup-only: `GET /v3/events/:event_id/`, `GET /v3/venues/:venue_id/events/`, `GET /v3/organizations/:organization_id/events/`. No endpoint returns "all public events matching a query"; the on-site search UI is internal-only. Lookup data richness is good (price, venue, capacity) but you must already possess the ID — useless for cold discovery.

**Luma's official API is management-only: no public discovery search and no self-RSVP endpoint, so BOTH Luma discovery AND registration run on the browser tier. [confirmed]**
Base `https://public-api.luma.com`, auth via `x-luma-api-key` header (calendar-scoped), requires a paid Luma Plus subscription. Rate limits: 200 req/min per calendar (key or OAuth token), 500 req/min per organization key; 429 on excess. The live `openapi.json` enumerates only owner/calendar-scoped endpoints — `/v1/events/get`, `/v1/calendars/events/list|lookup`, `/v1/organizations/events/list`, `/v1/events/guests/get|list`, plus ticket-types/coupons/webhooks. There is no cross-Luma public search and no endpoint by which a guest registers/RSVPs themselves; even management-side guest operations are scoped to a calendar the caller owns. A concierge acting AS an arbitrary attendee is browser-only for both discover and register.

**Meetup locked its API behind a paid Pro subscription and ~200 req/hour, but the GraphQL API exposes event search and RSVP mutations for authorized apps. (high confidence)**
Since the 2019 lockdown and Jan 31 2022 REST shutdown, all access is GraphQL only (`https://www.meetup.com/graphql/`). Creating an OAuth consumer requires an active Meetup Pro subscription; auth via `Authorization` bearer (OAuth, incl. server-to-server client-credentials for some public queries). Rate limit ~200 req/hour → `RATE_LIMITED`. Full schema introspection since Feb 2025. GraphQL supports mutations (create/RSVP), so programmatic RSVP is technically available to authorized apps, but the low cap and Pro-gating make it a costly, throttled source. Unauthenticated public GraphQL queries exist but are ToS-gray.

### Browser-only (and reverse-engineered fast-paths)

**Partiful and Resident Advisor have no official public API; both are reverse-engineer-or-browser targets. (non-load-bearing, high confidence)**
Partiful ships no documented API — its React/Expo app talks directly to Google Firebase; community wrappers (`github.com/cerebralvalley/partiful-api`) reverse-engineer create/update/delete/RSVP calls, and a Oct 2025 TechCrunch report showed raw Firebase objects reachable from browser devtools. Resident Advisor (`ra.co`) runs an internal GraphQL endpoint powering its own search that community tools hit unauthenticated (no official docs/support, ToS-gray). Treat both as browser-tier for reliability, with unofficial GraphQL as a brittle fast-path.

**Facebook/Meta Events is effectively dead for third-party discovery — the Events edge is heavily deprecated and the Groups API was fully removed in 2024. (non-load-bearing, medium confidence)**
Meta's deprecation wave (v19.0 Jan 2024 removed the entire Groups API on Apr 22 2024; continued through v21.0 Oct 2024) leaves the public Events edge without usable third-party read access. No supported path searches public FB events by keyword/geo as an external app. Browser-only, low-priority given login-walling and anti-automation defenses.

### Cross-source signals and meta-discovery

**schema.org/Event JSON-LD is the universal cross-source discovery signal on the browser tier and should be a first-class extraction target. [confirmed as canonical vocabulary]**
Event pages widely embed schema.org Event as JSON-LD in `<script type="application/ld+json">`; Google keys its Event rich results off it and supports subtypes MusicEvent, SocialEvent, TheaterEvent, SportsEvent, Festival, etc. Standard fields: `name`, `startDate`/`endDate` (ISO 8601), `location` (Place or VirtualLocation), `offers` (price, availability, url), `organizer`, `eventAttendanceMode`. Because it is normalized across Eventbrite, Luma, Dice, RA and long-tail venue/city calendars, a JSON-LD parser gives a uniform extraction schema for any browser-visited event page — the connective tissue for the hybrid model.

**Google Events results are not an official Google API; cross-source meta-discovery requires a SERP aggregator like SerpApi's `google_events` engine. (non-load-bearing, high confidence)**
No official Google Events API exists. SerpApi exposes `https://serpapi.com/search?engine=google_events` with `q` (e.g. "Events in Austin, TX"), `location`, date filters (today/week/weekend/online), and start-based pagination (10 results/page), returning normalized title, date, address, link, `ticket_info` (per-provider links), venue, thumbnail. It is a paid third-party proxy over Google's event knowledge panel that de-duplicates across Eventbrite/Meetup/Ticketmaster/venue sites — a broad top-of-funnel sweep before per-source enrichment/registration.

### Legal constraint on autonomous purchase

**REFUTED/ADJUSTED: Ticketmaster ToS and the federal BOTS Act make autonomous automated ticket PURCHASE on Ticketmaster/Live Nation legally hazardous — discovery is fine, checkout is not. [adjusted]**
The top-line thesis holds and is reinforced by active litigation, but three supporting details were corrected in fact-check and the ToS-vs-statute distinction is load-bearing for our policy engine:

- **ToS (contract):** Ticketmaster's Terms of Use prohibit *any* use of automated software/bots to search, reserve, or purchase tickets, and prohibit circumventing purchase-limit access controls. Autonomous automated checkout breaches the ToS regardless of intent.
- **BOTS Act (statute, 15 U.S.C. 45c, FTC/DOJ-enforced):** makes it illegal to bypass posted purchase limits or security/access-control measures by technical means, to use fictitious accounts to do so, and to resell tickets so obtained. By its text it does *not* ban all automated purchasing per se — a single within-limit automated checkout that does not defeat a CAPTCHA/access control or exceed a posted limit is a weaker statutory target (though still a ToS breach). The original claim conflated the contractual ban with the statutory scope.
- **Enforcement is active:** FTC brought its first BOTS Act cases in 2021 (broker judgments totaling ~$31M — Just In Time $11.2M, Concert Specials $16M, Cartisim $4.4M, largely suspended). In **September 2025** (not 2024) the FTC plus seven states sued Live Nation/Ticketmaster — the first case alleging both FTC Act and BOTS Act violations by a major platform; notably it targets TM's *failure to stop* brokers (five brokers, 6,345 accounts, 246,407 tickets), not TM using bots. Litigation is ongoing into 2026 (Live Nation motion-to-dismiss filings May 2026).
- **Penalty:** current per-violation civil maximum is **$53,088** (Jan 17 2025 inflation adjustment), not the stale $16k figure, with a further 2026 adjustment.

Bottom line for the product: event discovery is low-risk; fully autonomous paid checkout on TM/Live Nation is legally and contractually hazardous.

## Design implications

1. **Encode a three-band source tier list in the port/adapter layer.** (A) Clean-search API adapters — Ticketmaster Discovery v2 (incl. Universe), SeatGeek (partner-gated), Bandsintown/Songkick (read-only). (B) Management-only or Pro-gated API + browser RSVP — Meetup (GraphQL, Pro, RSVP mutation possible), Luma (Plus, mgmt only, RSVP via browser). (C) Browser-only for both discovery and registration — Eventbrite discovery, Luma register, Partiful, Resident Advisor, Facebook/Meta Events, most city/venue calendars. Each source is ONE port with an api-adapter and/or browser-adapter behind it.

2. **Model autonomous purchase as a per-source POLICY capability, not a global one.** Because TM ToS + the BOTS Act criminalize/breach automated purchase and limit-circumvention, the registration engine must consult per-source `automation_allowed` and `paid_allowed` flags. TM-family paid checkout is DISALLOWED by default (discovery-only). Paid autonomy is reserved for sources whose ToS/flow permit agent-driven attendee registration (Luma/Partiful/Meetup free RSVP; general web checkout using the user's own stored credentials, within posted limits, no access-control circumvention) — with per-user spend caps and an explicit within-limit / no-CAPTCHA-defeat guard baked into the checkout adapter.

3. **Make schema.org/Event JSON-LD the canonical internal event schema.** The browser-tier discovery adapter attempts JSON-LD extraction first (`name`, `startDate`/`endDate` ISO-8601, `location`/VirtualLocation, `offers.price`/`availability`/`url`, `organizer`, `eventAttendanceMode`) before falling back to DOM/vision parsing. This normalizes Eventbrite, Luma, RA, Dice, and long-tail venue pages into one ranking-ready shape and minimizes per-source parsing code.

4. **Budget for hard API rate/pagination ceilings in the discovery scheduler.** Ticketmaster 5000/day + 5 rps + 1000-item deep-paging cap; Meetup ~200 req/hour (Pro); Luma 200/min-calendar, 500/min-org. The retrieval planner must shard queries by geo/date/classification to stay under the 1000-item TM window and rate-gate/cache Meetup aggressively. No flow may assume unbounded pagination.

5. **Add a SerpApi `google_events` (or equivalent SERP) adapter as a cheap cross-source top-of-funnel sweep** that de-duplicates across providers, then route each candidate to the correct per-source enrichment/registration adapter via its `ticket_info` link. This decouples broad discovery from per-source registration and finds events on sources with no search API.

6. **Gate partner-approval sources behind capability feature-flags** tied to whether valid partner/Pro credentials exist for the tenant (SeatGeek, Bandsintown, Songkick, Dice partner GraphQL, Meetup Pro). The system degrades gracefully to Ticketmaster + browser-tier when credentials/approval are absent — never hard-fail discovery.

7. **Model per-source credential + subscription requirements as first-class in the multi-tenant credential vault.** Luma needs a per-user/per-calendar `x-luma-api-key` AND (for register) a stored Luma login for browser RSVP; Meetup needs an OAuth consumer + Pro; Bandsintown needs an org-level `app_id` under signed consent. The stored-credential schema must distinguish org-level API keys (shared) from per-user login secrets (browser-tier), with tenant isolation on both.

## Sources

- [Eventbrite Platform API Reference](https://www.eventbrite.com/platform/api) — no public event-search; only ID/venue/org lookup endpoints survive.
- [Eventbrite v3 Search deprecation (Automattic issue #83)](https://github.com/Automattic/eventbrite-api/issues/83) — Dec 2019 removal, Feb 20 2020 whitelist cutoff for `GET /v3/events/search/`.
- [Ticketmaster Discovery API v2](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/) — `/discovery/v2/events.json`; 5000/day, 5 rps, 1000-item deep-paging cap; `source=universe` filter.
- [Universe Developer Portal](https://developers.universe.com/) — TM-owned; surfaces via Discovery API; GraphQL/REST + OAuth2 for organizers.
- [Ticketmaster Terms of Use](https://legal.ticketmaster.com/terms-of-use/) — prohibits bots/automated software to search, reserve, or purchase; anti-circumvention of purchase limits.
- [FTC — BOTS Act compliance refresher (2025)](https://www.ftc.gov/business-guidance/blog/2025/04/bots-act-compliance-time-refresher) — FTC-enforced ban on circumventing purchase limits/security.
- [FTC — first BOTS Act cases (2021)](https://www.ftc.gov/news-events/news/press-releases/2021/01/ftc-brings-first-ever-cases-under-bots-act) — ~$31M combined broker judgments.
- [FTC 2025 inflation-adjusted civil penalties](https://www.ftc.gov/news-events/news/press-releases/2025/02/ftc-publishes-inflation-adjusted-civil-penalty-amounts-2025) — $53,088/violation max.
- [FTC/states sue Live Nation/Ticketmaster (Sep 2025)](https://news.pollstar.com/2025/09/18/ftc-sues-live-nation-ticketmaster-for-illegal-ticket-resale-tactics-bots-act-violations/) — first FTC Act + BOTS Act case against a major platform; ongoing 2026.
- [Better Online Ticket Sales Act (Wikipedia)](https://en.wikipedia.org/wiki/Better_Online_Tickets_Sales_Act) — 2016 law; automated limit-bypass illegal.
- [Luma API — Getting Started](https://docs.luma.com/reference/getting-started-with-your-api) — base `public-api.luma.com`, `x-luma-api-key`, Luma Plus required, calendar-scoped.
- [Luma OpenAPI spec](https://public-api.luma.com/openapi.json) — owner/mgmt-only endpoints; no public search, no self-RSVP.
- [Luma Help — API rate limits](https://help.luma.com/p/luma-api) — 200 req/min per calendar, 500 req/min per org key.
- [Meetup GraphQL API — Introduction](https://www.meetup.com/graphql/) — GraphQL-only since REST shutdown Jan 31 2022; introspection since Feb 2025; supports mutations.
- [Meetup GraphQL — Authentication](https://www.meetup.com/graphql/authentication/) — OAuth bearer; Pro required for OAuth consumer; ~200 req/hour.
- [Meetup — API capabilities & limitations](https://help.meetup.com/hc/en-us/articles/41455194927373-What-can-I-achieve-through-Meetup-s-API-and-what-are-its-limitations) — Pro-gated automation scope.
- [Partiful unofficial API (cerebralvalley/partiful-api)](https://github.com/cerebralvalley/partiful-api) — no official API; Firebase backend reverse-engineered for CRUD/RSVP.
- [Resident Advisor ra.co GraphQL (gist)](https://gist.github.com/mkmeral/1f690db13937d89e0d358a938c32cd11) — internal GraphQL hit unauthenticated; no official docs/support.
- [Meta Graph API Changelog](https://developers.facebook.com/docs/graph-api/changelog) — Groups API removed Apr 22 2024; Events edge unusable for 3rd-party discovery.
- [Bandsintown API documentation](https://help.artists.bandsintown.com/en/articles/9186477-api-documentation) — read-only; `app_id`; requires written consent + partnership.
- [Songkick Developer / Concerts & Festivals API](https://www.songkick.com/developer) — partner live-music discovery; discovery-only.
- [Dice Ticket Holders GraphQL (partners)](https://partners-endpoint.dice.fm/graphql/docs/index.html) — partner-scoped organizer GraphQL only; no public attendee API.
- [schema.org/Event](https://schema.org/Event) — canonical Event vocabulary; JSON-LD; subtypes MusicEvent/SocialEvent/etc.
- [Google Event structured data docs](https://developers.google.com/search/docs/appearance/structured-data/event) — Event rich results driven by schema.org Event JSON-LD.
- [SerpApi Google Events API](https://serpapi.com/google-events-api) — `engine=google_events`; normalized title/date/venue/`ticket_info`; 10/page; no official Google API.
- [SeatGeek Platform / Build](https://seatgeek.com/build) — Platform API exists (OAuth client, `/events` search); access trending partner-gated.
- [SeatGeek Platform Terms of Use](https://seatgeek.com/api-terms) — governs data use; supports partner-gated positioning.
