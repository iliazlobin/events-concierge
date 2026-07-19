# Source-Tier Re-Verification (2026): Act-on-Behalf RSVP Auth, Rate Limits, and the Per-Source API-vs-Browser Fork

> Closes the wave-1 gap where the source tier list, the Meetup rate-limit figure, and the act-on-behalf RSVP auth model were unverified assumptions. This is load-bearing for the SourcePort adapter matrix, the credential vault schema, and which register()/discover() paths are allowed on the reliability SLA versus relegated to browser best-effort with human handoff.

## Verified findings

### Meetup — the only API-based register() on the SLA critical path

**[confirmed]** Meetup GraphQL exposes `createEventRsvp(input: CreateEventRsvpInput)` with fields `eventId` and `response`; compliant act-on-behalf RSVP is possible **only** via per-user OAuth 2.0 authorization-code ("Server Flow"), never via a single Pro/organizer key acting on strangers.
- The authentication page states verbatim for the JWT server-to-server flow: "member authorization is limited to the owner of the OAuth Client" — a single server key cannot act as arbitrary users.
- The Server Flow is the prescribed per-user path: redirect each user to `https://secure.meetup.com/oauth2/authorize`, exchange the code at `https://secure.meetup.com/oauth2/access` for a bearer access token (~3600s TTL) plus a refresh token.
- Behavioral contract confirmed by Meetup staff (Doug Tangren) in the API group: **omit `member_id`** to RSVP the OAuth-authenticated member; passing an explicit `member_id` implies you are the org/host RSVPing on their behalf, and if the token owner is not an org/host you get **401**. Even organizers cannot RSVP members who have not authorized the app.
- Mutation shape confirmed via introspection: `mutation RSVPToEvent($input: CreateEventRsvpInput!) { createEventRsvp(input: $input){ errors{code message} rsvp{id response created} } }`.
- Minor caveat: the `WAITLIST` response enum value was not observed in primary sources (only `YES`/`NO` seen); does not affect the auth conclusion.

**[confirmed]** Meetup's current documented rate limit is **500 points per rolling 60 seconds** (leaky-bucket) — supersedes the wave-1 "~200 points/req-hr" figure in dossier 03.
- Official GraphQL guide, verbatim: "The API currently allows you to have 500 points in your queries every 60 seconds."
- Throttle errors return "Too many requests, please try again shortly." with an `extensions` object carrying `consumedPoints` and `resetAt` — use these for adaptive backoff rather than a fixed sleep.
- The prior ~200/req-hr number is not a per-hour policy and is stale; replace it in dossier 03.

**[adjusted]** Creating the OAuth consumer (our app) requires an active Meetup **Pro** subscription on the developer/product account; end users who authorize the app do **not** need Pro. Upgraded from wave-1 "medium-confidence secondhand" to confirmed: this has been Meetup policy since the Aug 2019 OAuth transition and remains in force. (The specific help-center article returned HTTP 403 to automated fetching but the requirement is corroborated across current help-center API-access articles.)

### Luma — browser-only for both discover() and register()

**[adjusted]** *Corrected claim:* Luma (lu.ma events) has no self-RSVP/act-on-behalf endpoint. The public REST API (base `https://public-api.luma.com`) is API-key auth via the `x-luma-api-key` header, scoped so "each key only grants access to the calendar it was created on" — an owner-account credential, not delegated end-user OAuth. Write endpoints (add guests, update guest status, add host) act only on the key-owner's own events, so autonomous RSVP to a third party's Luma event is browser-only. There is no public event-search/discovery endpoint (only owner-scoped list/lookup calls).
- Two corrections to supporting detail (conclusion unaffected):
  1. Endpoint paths in the wave-1 write-up are stale — get-self is `/v1/users/get-self` (plural); the cited `update-guest-status` reference URL now 404s. Equivalent capabilities still exist.
  2. Luma's rate-limit doc references "Calendar API keys and OAuth tokens: 200 req/min per calendar" — so it is imprecise to say Luma has zero OAuth surface, but that token is not a user-delegation/act-on-behalf flow (Nango confirms API_KEY-only integration) and does not enable third-party self-RSVP.
- Third parties resort to scrapers (e.g. Apify Luma Events Scraper), confirming the discovery gap.
- Note: "Luma Uni API" / `LUMA_UNI_API_KEY` / OAuth2 developer-portal references are Luma **AI** (lumalabs.ai, video/3D generation) — a different product — and are noise.

### Eventbrite — browser-only for both discover() and register()

**[confirmed]** Eventbrite offers no API to place a free order / register an attendee, and its public event Search API remains dead.
- Eventbrite staff, in the API group: there is no method for ordering tickets; alternatives are the checkout/registration widget or manually adding attendees in the backend. The Orders API is read/manage-only for an organizer's existing orders — no POST-create-order / programmatic-registration endpoint. No 2025/2026 changelog reintroduces order creation.
- `GET /v3/events/search/` was removed effective Dec 12, 2019 (creator whitelist expired Feb 20, 2020) and has not returned. Events can only be fetched by event ID (`/v3/events/:id/`), venue (`/venues/:id/events/`), or organization (`/organizations/:id/events/`) — a management API for your own org, not a public directory.
- Conclusion: free RSVP orders must go through the web registration flow (Computer Use + DOM adapter); discovery must come from SerpApi/browser.

### Ticketmaster — first-class SLA-grade discovery (read-only)

**[confirmed]** *(non-load-bearing, researcher confidence: high)* Ticketmaster Discovery API v2 is self-serve, SLA-grade discovery: default **5000 calls/day**, **5 requests/second**, deep paging capped at the **1000th item**; read-only (no RSVP). Higher quotas grantable on compliance with terms/branding. Shard queries by date/geo to exceed 1000 results. Anchors the API-first discovery tier alongside Meetup.

### Secondary / gated discovery sources (read-only, no RSVP)

The following are usable niche discovery adapters but are onboarding-gated and must not be launch-critical:
- *(confidence: medium)* **SeatGeek** Platform API (events/performers/venues via `client_id`) is approval-gated / partner-oriented; accounts sit in "pending approval." Supplementary discovery behind Ticketmaster; revenue-share Partner Program (~$11/sale).
- *(confidence: high)* **Bandsintown** public API is artist/promo-scoped (read-only) via an `app_id` obtained through Bandsintown for Artists.
- *(confidence: high)* **Songkick** is paid-license partnership only ("subject to the standard terms of our partnership agreement and a license fee"; "not approving API requests for student projects, educational purposes or hobbyist purposes"); read-only, 6M+ concerts.

### Cross-source discovery funnel

**[confirmed]** *(non-load-bearing, researcher confidence: high)* SerpApi `engine=google_events` returns structured title, date, address, link, ticket_info, venue, thumbnail at ~$0.025/search. Paid plans $25/1k ($75/5k, $150/15k, $275/30k), free tier 250/mo, one unified monthly allowance across all engines. This is the practical top-of-funnel for the browser-only sources (Luma, Partiful, Eventbrite) that have no discovery API.

### Partiful — browser-only, no official API

*(researcher confidence: medium)* Partiful offers no official public API or keys. Community integrations reverse-engineer network endpoints and require pulling an auth token from the browser session (e.g. Playwright login). There is no sanctioned act-on-behalf mechanism. Both discover() and register() are browser best-effort with human handoff. Reverse-engineered endpoints are brittle and ToS-risky — a maintained DOM adapter over the real RSVP flow is safer than depending on the unofficial API.

## Design implications

### SourcePort adapter matrix (hard-code for launch)

| Source | discover() | register() | On reliability SLA? |
|---|---|---|---|
| Meetup | API (GraphQL) | **API only** (per-user OAuth, `member_id` omitted) | **register() = yes** |
| Ticketmaster | API (SLA-grade) | n/a (read-only) | discover() = yes |
| SeatGeek | API (gated/secondary) | n/a | no (feature-flag gated) |
| Bandsintown | API (artist-scoped) | n/a | no |
| Songkick | API (paid partner) | n/a | no (signed-license gated) |
| SerpApi google_events | API (cross-source funnel) | n/a | discover() = yes |
| Luma | browser / SerpApi only | browser (Computer Use + DOM) | no — best-effort |
| Eventbrite | browser / SerpApi only | browser (Computer Use + DOM) | no — best-effort |
| Partiful | browser only | browser (Computer Use + DOM) | no — best-effort |

Only **Meetup register()** sits on the reliability-SLA critical path. Every browser register() path is best-effort with circuit-breakers and human-handoff on failure.

### Credential vault — two distinct credential shapes

Model as two separate ports; do **not** attempt a single organizer key for act-on-behalf (it 401s for non-hosts):
- **OAuth port (Meetup):** per-user authorization-code tokens (access + refresh), act-as-self with `member_id` omitted, obtained via a "Connect your Meetup account" flow. Handle refresh on the ~3600s access-token TTL.
- **Session-capture port (Luma / Partiful / Eventbrite):** per-user browser session material (cookies/session tokens) captured during a user-driven login handoff.

### Meetup provisioning and rate limiting

- Provision **one** product-owned Meetup Pro subscription + OAuth consumer at the tenant/platform level (Pro required to create the consumer); fan out per-user authorization-code grants. Users need not be Pro.
- Build a **global leaky-bucket limiter of 500 points / 60s** in front of the Meetup adapter, shared across tenants. Account point cost per query (not the stale ~200/hr assumption). Drive backoff off the `consumedPoints`/`resetAt` throttle extensions.

### Ticketmaster discovery scheduler

- Enforce **5 rps** and **5000/day per key**. Design pagination to **shard by date/geo** because deep paging dies at the 1000th item — never assume an unbounded walk. Apply for a raised quota early (compliance-gated).

### Browser-only sources

- Wire SerpApi `google_events` (~$0.025/search) as the default top-of-funnel for Luma / Eventbrite / Partiful; route their register() exclusively through the browser adapter. Circuit-breakers + human-handoff; keep OFF the SLA.
- Do not depend on unofficial reverse-engineered Partiful endpoints — prefer a maintained DOM adapter over the real RSVP flow.

### Gated adapters

- Gate SeatGeek and Songkick behind a feature flag + signed-agreement check (SeatGeek partner approval; Songkick paid license, hobbyist-rejected). Launch discovery floor = **Ticketmaster + Meetup + SerpApi**.

## Sources

- [Meetup GraphQL API Guide](https://www.meetup.com/graphql/guide/) — verbatim "500 points in your queries every 60 seconds"; `createEventRsvp` in schema; `consumedPoints`/`resetAt` throttle extensions.
- [Meetup GraphQL Authentication](https://www.meetup.com/graphql/authentication/) — authorization-code Server Flow = act-as-member; JWT flow limited to OAuth client owner.
- [Meetup API group — 401 creating RSVP for event](https://groups.google.com/g/meetup-api/c/h8p2Krw7PSA) — omit `member_id` to RSVP as authenticated member; explicit `member_id` needs org/host or 401.
- [Meetup Help — How can I get access to Meetup's API?](https://help.meetup.com/hc/en-us/articles/41453576628749-How-can-I-get-access-to-Meetup-s-API) — Pro required to create OAuth consumers (403 to automated fetch; corroborated across help-center articles).
- [meetupr introspection vignette](https://cran.r-project.org/web/packages/meetupr/vignettes/introspection.html) — exact `createEventRsvp` mutation shape.
- [Luma API Getting Started](https://docs.luma.com/reference/getting-started-with-your-api) — `x-luma-api-key`, per-calendar scope.
- [Luma Help — Luma API](https://help.luma.com/p/luma-api) — "each key only grants access to the calendar it was created on."
- [Luma OpenAPI spec](https://public-api.luma.com/openapi.json) — only owner-scoped list/lookup reads; no public search.
- [Nango — Luma integration](https://nango.dev/docs/integrations/all/luma) — confirms API_KEY-only auth mode (no user-delegation OAuth).
- [Eventbrite API group — order tickets via API](https://groups.google.com/g/eventbrite-api/c/_d7KBUoh_7Q) — no API method to order/register; use registration flow or manual backend add.
- [Eventbrite v3 Search API deprecation (Automattic #83)](https://github.com/Automattic/eventbrite-api/issues/83) — `/v3/events/search/` removed Dec 12 2019; still dead in 2026.
- [Eventbrite Orders docs](https://www.eventbrite.com/platform/docs/orders) — read/manage-only for existing orders.
- [Ticketmaster Discovery API v2](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/) — 5000/day, 5 rps, deep paging capped at 1000th item; self-serve, read-only.
- [SeatGeek Build](https://seatgeek.com/build) — Platform REST API via `client_id` + Partner Program; approval-gated.
- [SeatGeek api-support #169](https://github.com/seatgeek/api-support/issues/169) — evidence of manual approval gating ("pending approval").
- [Songkick Developer](https://www.songkick.com/developer) — partnership + license fee; no student/hobbyist; read-only 6M+ concerts.
- [Bandsintown API docs](https://help.artists.bandsintown.com/en/articles/9186477-api-documentation) — artist-promo-scoped read-only via `app_id`.
- [SerpApi Google Events API](https://serpapi.com/google-events-api) / [Pricing](https://serpapi.com/pricing) — structured event fields; $25/1k, free 250/mo, unified allowance.
- [Partiful unofficial API](https://github.com/cerebralvalley/partiful-api) — no official API; needs browser-session token; browser-only for discover and register.
