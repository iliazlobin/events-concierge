# Throughput and Capacity Model Against Per-Source Quotas and Anthropic Org Limits (2026)

> Closes the post-wave-1 gap of "what actually binds first as free RSVP scales" — replacing the naive assumption that Meetup is the wall with a quantified, per-source binding-ceiling model that reorders the architecture (shared crawl + per-user tokens + Anthropic Scale tier), with browser concurrency as the real mid-scale ceiling.

## Verified findings

### Anthropic org limits (2026) [confirmed]

- Anthropic tiers are **Start / Build / Scale / Custom**. [confirmed]
  - **Start**: 1,000 RPM / 2M ITPM / 400k OTPM (Opus 4.x, Sonnet 5, Sonnet 4.x, Haiku 4.5 all equalized); spend cap $500.
  - **Build**: 5,000 RPM / 5M ITPM / 1M OTPM; spend cap $1,000.
  - **Scale**: 10,000 RPM / 10M ITPM / 2M OTPM per model class; spend cap $200,000/mo.
  - **Custom**: above Scale, no fixed spend cap.
- **Cache-read tokens are EXCLUDED from ITPM.** [confirmed] Verbatim: "For most Claude models, only uncached input tokens count towards your ITPM rate limits." `input_tokens` (after the last cache breakpoint) + `cache_creation_input_tokens` COUNT; `cache_read_input_tokens` do NOT. Worked example verbatim: "With a 2,000,000 ITPM limit and an 80% cache hit rate, you could effectively process 10,000,000 total input tokens per minute." Only the retired Haiku 3.5 (†) counts cache reads toward ITPM. [confirmed]
- **Bucketing nuance** [confirmed]: Sonnet 5 has its OWN rate-limit bucket, separate from the combined Sonnet 4.x bucket (Sonnet 4.x pools 4.6+4.5; Opus pools 4.8/4.7/4.6/4.5). The per-model numbers above are correct; they are carried in independent buckets, so Sonnet 5 and Sonnet 4.x can each run at full limit concurrently.
- **Batch API has its own limits, shared across models** [confirmed]: at Scale, 4,000 RPM and 500,000 batch requests in the processing queue. Batch traffic does not draw down the interactive Messages ITPM/OTPM.
- **Mechanics** [confirmed]: token-bucket algorithm; 429 returns a `retry-after` header; limits are ORG-level with per-workspace sub-limits (workspace sharding). Live backpressure via `anthropic-ratelimit-*` response headers and the Rate Limits API.
- **Fable 5** sits in a lower bucket: Scale = 4,000 RPM / 4M ITPM / 800k OTPM. [confirmed]
- The "restructured June 2026" framing is not stated in the doc and is immaterial; the tier names and numbers are what is confirmed.

### Meetup GraphQL quota [adjusted]

- **500 query-POINTS per 60 seconds** — a complexity/points budget, not per-request. [confirmed] Exceeding it returns error code `RATE_LIMITED` with `consumedPoints:500` and a `resetAt` timestamp, naming the offending query; points scale with query complexity. [confirmed]
- **ADJUSTED — the per-token scoping is an unverified assumption.** Meetup's public docs do NOT state what the 500-point window is scoped to (per token vs per app/OAuth-consumer vs per IP). The architectural conclusion — that a single shared app credential is a hard central wall while per-user OAuth tokens make discovery "effectively unbounded per user" — rests entirely on the assumption that the quota buckets per token. That is a plausible inference (many GraphQL APIs bucket per authenticated client) but is **unverified**. If Meetup buckets per app/consumer or per IP, per-user tokens would NOT relieve the central wall. **Validate empirically against the live API before relying on it in the capacity model.** [adjusted]
- **The "~10-20 points per city+category search" cost is an unpublished estimate.** [unverifiable] Meetup does not document per-query point costs. The downstream "~25-50 searches/min globally" and "saturates at low thousands of users" figures inherit this estimate — treat as an order-of-magnitude planning figure to measure, not a fact.
- RSVP write **mutations require an authenticated user token** and so are inherently per-user regardless of how reads are scoped. [confirmed by construction]

### Luma and Ticketmaster [confirmed]

- **Luma** (per calendar): GET 500 / 5 min; POST 100 / 5 min (tracked separately). RSVP is a POST → **~20 writes/min per calendar**. Over-limit returns 429 and blocks further requests for a full 1 minute. Fine for an individual user's own calendar; a shared service calendar throttles fast. Docs note limits "subject to change." [confirmed]
- **Ticketmaster Discovery API**: default 5,000 calls/day, 5 requests/second, hard deep-paging cap `size × page < 1000` (cannot retrieve past the 1,000th item). A **catalog-completeness constraint, not per-user** — forces narrow, faceted queries. Increases case-by-case after ToS/branding review. [confirmed]

### Browserbase managed-browser concurrency [confirmed]

- Concurrent-session ceilings: **Free 3 / Developer 25 / Startup 100 / Scale 250+**. [confirmed]
- Session-**creation** rate caps: **5 / 25 / 50 / 150 per minute** (Free/Dev/Startup/Scale). [confirmed] Docs verbatim confirm creation is independent of concurrency: "if you attempt to create too many sessions in a short time, you might temporarily hit your cap, even if you haven't reached max concurrency."
- Filling 100 Startup sessions at 50/min takes ~2 minutes — so a Friday burst of browser-only RSVPs queues even below the concurrency ceiling. [confirmed derivation]
- Overage: Developer $0.12/browser-hr, Startup $0.10/browser-hr. [confirmed]
- Only browser-only sources consume this pool; API-clean RSVP bypasses it entirely.

### Eventbrite [medium confidence, not load-bearing]

- Current default reported as **~5,000 requests/hour** for new keys (historically 1,000/hr + 48,000/day cap); increases case-by-case. The primary rate-limits page is JS-rendered and could not be fetched cleanly, so the exact current number is medium confidence. At either level, Eventbrite does not bind before Meetup(naive)/Browserbase. Eventbrite discovery is browser-best-effort; its free-event RSVP is API-capable.

### Capacity model — first binding ceilings [medium confidence, load-bearing]

Explicit assumptions: M = 3 requests/user/week; the Friday-evening burst concentrates ~15% of weekly volume in the single busiest hour (~25x a flat hour). Per request: ~3-4 API discovery reads, 1 Cohere rerank, ~2 Claude calls (Haiku parse + Sonnet select over top-10, ~6k uncached input each); RSVP conversion ~0.4/request, of which ~30% are browser-only; browser session ~90s; RSVP agentic loop ~5 Sonnet vision turns.

Peak-hour user requests: 1k users -> 450/hr (7.5/min); 10k -> 4,500/hr (75/min); 100k -> 45,000/hr (750/min ~= 12.5/s).

- **(a) Source (naive path).** On a SHARED Meetup token, naive per-request discovery binds first — at ~10k peak users (75/min x ~15 pts = 1,125 pts/min) it is ~2.25x over the 500 pts/60s budget, walling around **~4-5k users**. (Depends on the unverified per-token/point-cost assumptions above.) Fix: scheduled shared crawl into your own schema.org/Event index + per-user OAuth for anything user-specific, which removes source quota from the user-scaling path.
- **(b) Anthropic.** At 100k peak, Sonnet ITPM ~= 750/min x 6k ~= 4.5M ITPM (fits Scale 10M, exceeds Build 5M); RPM ~= 3,000 total, far under the 10k/model cap. Vision turns are largely cacheable (tool defs/system); the non-interactive ranking/summary leg can move to Batch. Anthropic binds **~200-400k users**, mitigated by Scale tier + caching + Batch + workspace sharding, then Custom.
- **(c) Browser.** Peak concurrent sessions = requests/hr x 0.4 x 0.3 x (90/3600): 1k -> 1.4; 10k -> 13.5 (fits Developer 25); 100k -> 135 (exceeds Startup 100 -> Scale 250+ or self-host).
- **(d) First-binding order after mitigations:** Browserbase concurrency bites at **~75-100k peak users, before Anthropic ITPM**.
- **Cost per confirmed RSVP:** API path ~$0.05-0.12; browser path ~$0.15-0.40; blended ~$0.10-0.20. (Claude discovery ~$0.01-0.03 + browser RSVP loop Sonnet vision ~$0.05-0.30 + Browserbase ~90s ~$0.0025 + Cohere ~$0.001.)

## Design implications

Directives for the Events Concierge:

1. **Decouple discovery from per-request source calls.** Run a scheduled, deduplicated shared CRAWL of API-clean sources (Ticketmaster / SeatGeek / Meetup / Bandsintown) into your own pgvector-indexed schema.org/Event store. N users x M requests must NOT fan out to N*M source calls. Source read-quota then scales with CATALOG breadth (geos x categories x refresh cadence), not user count — this is what actually clears the Meetup wall.
2. **Route every user-specific action through the requesting user's OWN stored OAuth token.** RSVP writes MUST be per-user-token by construction. For reads, this is the mitigation for per-source quotas *if* they bucket per token — but see the Meetup caveat: validate the scoping empirically before treating per-user tokens as the source-quota escape hatch.
3. **Put a global fair-share scheduler / token-bucket in front of each source adapter, keyed by (source, credential).** Enforce Meetup points-budget, Ticketmaster 5 rps + 5,000/day, Luma 100 POST/5min/calendar; honor 429 `retry-after` and the 1-min blocks. Shape the Friday burst into a per-user-fair queue instead of hitting quotas synchronously.
4. **Respect Ticketmaster deep-paging** (`size x page < 1000`) in the crawler — never page past 1,000 results; partition by city/date/segment facets to cover the catalog, since completeness is bounded per-query not per-day.
5. **Provision Anthropic on Scale and lean hard on prompt caching.** Cache tool defs, system prompts, Computer-Use scaffolding, and candidate context — cache reads are excluded from ITPM, giving ~5x effective ITPM at 80% hit rate. Move non-interactive legs (bulk ranking/summarization, re-scoring) to the Batch API to protect interactive Messages ITPM.
6. **Shard Anthropic load across per-tenant or per-function workspaces with sub-limits;** plan the Custom-tier / multi-org path for the >200-400k-user regime. Use the Rate Limits API + `anthropic-ratelimit-*` headers for live backpressure.
7. **Treat managed-browser concurrency as the mid-scale ceiling.** Keep browser-only RSVP (Partiful / Luma-registration / Eventbrite best-effort) OFF the reliability-SLA critical path. Set the self-host threshold at ~100 concurrent sessions (Browserbase Startup ceiling / ~75-100k peak users); beyond that, run an autoscaling self-hosted Chromium fleet. Absorb bursts against the session-CREATION cap (50/min Startup), not just max concurrency.
8. **Prefer API-clean RSVP paths** (Meetup mutation, Eventbrite order API) over browser automation wherever supported — they bypass the browser pool, are cheaper (~$0.05-0.12 vs ~$0.15-0.40), and are SLA-eligible.
9. **Track per-confirmed-RSVP cost as a first-class SLO metric.** Route the RSVP agentic loop to the cheapest sufficient tier: Haiku for parsing/DOM classification, Sonnet for vision Computer-Use turns, Opus only on hard fallbacks — to keep blended cost near $0.10-0.20.

### Per-source quota matrix

| Source | Quota | Scope | Bind type | Our posture |
|---|---|---|---|---|
| Meetup GraphQL | 500 points / 60s | Unconfirmed (assumed per-token) | Central if shared token | Crawl for discovery; per-user token for RSVP mutations; validate scoping |
| Luma | GET 500 / POST 100 per 5 min | Per calendar | Write-tight (~20 RSVP/min/cal) | Per-user calendar; 429 -> 1-min block backoff |
| Ticketmaster | 5,000/day, 5 rps, size*page<1000 | Per app key | Catalog completeness | Faceted crawl; never deep-page |
| Browserbase | Concurrency 100 (Startup); creation 50/min | Per account | Mid-scale ceiling | Self-host at ~100 concurrent; keep off SLA path |
| Eventbrite | ~5,000/hr (medium conf.) | Per key | Non-binding | Browser best-effort discovery; API RSVP for free events |
| Anthropic | Scale 10k RPM / 10M ITPM / 2M OTPM per model | Org (workspace sub-limits) | Binds ~200-400k users | Scale tier + caching + Batch + workspace sharding -> Custom |

### First-binding ceiling summary (after mitigations)

| Regime (peak users) | Binding constraint | Action |
|---|---|---|
| ~4-5k | Meetup (naive shared-token discovery ONLY) | Ship shared crawl + per-user tokens; removes this |
| ~75-100k | Browserbase browser concurrency | Move to Scale 250+ or self-host Chromium fleet |
| ~200-400k | Anthropic Sonnet ITPM | Caching + Batch + workspace sharding, then Custom tier |

## Sources

- [Claude Platform Docs — Rate limits](https://platform.claude.com/docs/en/api/rate-limits) — Primary, fetched. Start/Build/Scale/Custom tiers; Scale = 10k RPM / 10M ITPM / 2M OTPM per model; cache_read excluded from ITPM (80%-hit worked example -> 10M effective); Batch API separate shared limits (Scale 4k RPM, 500k queue); token-bucket, 429 retry-after, org-level limits with workspace sub-limits; Sonnet 5 in its own bucket. Confirmed.
- [Meetup GraphQL API Doc Guide](https://www.meetup.com/graphql/guide/) — Primary, fetched. "500 points in your queries every 60 seconds"; RATE_LIMITED error with consumedPoints/resetAt; points scale with complexity. Does NOT document quota scoping (per-token vs per-app vs per-IP) or per-query point costs — both flagged as assumptions to validate. See also [graphql root](https://www.meetup.com/graphql/), [support](https://www.meetup.com/graphql/support/).
- [Luma API — Rate Limits](https://docs.luma.com/reference/rate-limits) — Primary, fetched. GET 500 / POST 100 per 5 min per calendar (separate); 429 blocks 1 minute; limits subject to change. Confirmed.
- [Ticketmaster Discovery API v2](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/) — Primary. 5,000 calls/day + 5 rps; deep-paging cap size*page<1000; increases case-by-case. Confirmed.
- [Browserbase — Plans and Pricing](https://docs.browserbase.com/guides/plans-and-pricing) — Primary, fetched. Concurrency 3/25/100/250+; creation 5/25/50/150 per min (independent of concurrency, per docs); overage $0.10-0.12/browser-hr. Confirmed; corroborated by third-party 2026 listings.
- [Eventbrite Platform — Rate Limits](https://www.eventbrite.com/platform/docs/rate-limits) — Primary page JS-rendered, not cleanly fetchable; ~5,000 req/hr new keys per secondary summaries (historically 1,000/hr + 48,000/day). Medium confidence.
- [Rollout — Eventbrite API Essentials](https://rollout.com/integration-guides/eventbrite/api-essentials) — Secondary corroboration of Eventbrite hourly limiting.
