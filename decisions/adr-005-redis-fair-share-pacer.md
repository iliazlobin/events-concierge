# ADR-005: Off-engine Redis fair-share pacer with the Meetup per-app quota contingency

**Status:** accepted — with owner-ratification riders (see Consequences) · **Date:** 2026-07-10

## Context

Every external source the concierge touches carries a hard, evidence-backed rate budget, and FR-10.4 makes pacing a single, named obligation: pace per-user, per-source traffic at human cadence behind a fair-share scheduler keyed by `(source, credential)` — Meetup 500 points/60s, Ticketmaster 5 rps + 5,000/day, Luma 100 POST/5 min per calendar. FR-7.6 leans on that same mechanism as half of the compliance posture (contract/tort discipline via human-cadence pacing plus the FR-10.3 quarantine breaker, never IP rotation). The question this ADR settles is the substrate: where the pacer lives, what it guards, and what happens when Meetup's quota turns out to be scoped per-app rather than per-token.

- **FR-10.4 / AC-73** bind the scheduler shape: `(source, credential)` keying, fair share across users, honoring `retry-after` and reset timestamps.
- **FR-3.5 / AC-24 / NFR-4a** bind credential scoping: per-user discovery (Meetup, Luma) always draws against the requesting user's own token, so per-user-source quota scales per tenant; Ticketmaster's single global app key is absorbed by the central cache and the crawler's daily budget.
- **G2 is a DESIGN-BLOCKING spike** (requirements §8): whether Meetup's 500pt/60s budget is per-token, per-app, or per-IP is unmeasured, and **NFR-2 explicitly defers the autonomous-lane SLA number** until G2 (and the G1 request-mix gate) lands. The design must carry the per-app contingency now, as configuration, not as a re-architecture.
- **AC-45** requires browser-pool saturation to route new browser RSVPs to handoff rather than queue past the SLA — so an admission cap for the ~135-slot browser pool needs a fast service-plane home.
- Load basis: ~750 req/min Friday peak, ~390 child registration starts/min, ~520k RSVPs/mo — external source quotas, not compute, are the binding resource.

## Options considered

1. **Off-engine Redis fair-share pacer (chosen).** A dedicated service holding token buckets in Redis keyed `(source, credential)`, deficit-round-robin fair share across users within a bucket, leases acquired by activities immediately before the wire call. Sub-millisecond acquire on the hot path; the G2 per-app contingency is a pure config flip (`quota_scope[meetup]=app`); the browser-pool admission cap and Luma 429 full-block bookkeeping land in the same component. The cost is a second stateful substrate next to the Temporal spine — accepted because the state is deliberately disposable (see Decision).

2. **Temporal-native quota entity workflows.** Per-`(source, credential)` RateLimiterWorkflows granting token-bucket leases via workflow Update, keeping every durable fact inside one execution model. Rejected because it puts an engine round-trip (~50–200 ms plus a billed action) in front of every source call — roughly three acquires per Meetup RSVP chain — and sprawls to ~100k long-lived entity workflows at scale, buying durability that pacing does not need. Rate-limiting here is politeness, not correctness: the fail-safe direction on state loss is throttle-first re-initialization, which a Redis bucket does trivially, whereas an entity workflow pays for exactly the persistence that is unwanted. The browser-pool admission cap needs a fast service-plane component regardless, so the engine-native option does not even eliminate the second substrate.

## Decision

We will run fair-share pacing as an **off-engine Pacer service backed by Redis token buckets keyed `(source, credential)`**, with these mechanisms and invariants:

- **Buckets and budgets:** `meetup:{user_token}` at 500 points/60s; `luma:{calendar}` at 100 POST/5 min, honoring a Luma 429 as a 1-minute full block on that calendar's bucket; `tm:{app_key}` at 5 rps + 5,000/day for the catalog crawler. All buckets honor `retry-after` and `resetAt` timestamps from the source (AC-73).
- **Fairness:** deficit-round-robin across users within a bucket, so one heavy user cannot starve others sharing a credential-scoped budget.
- **Lease-before-call:** every mutating activity and every per-user discovery activity acquires a lease from the Pacer immediately before its wire call. Catalog-backed reads (Ticketmaster cache, search funnel) never touch the Pacer on the interactive path.
- **No held workers:** when the projected wait for a lease is long, the activity returns the projection and the workflow converts it into a durable-timer backoff — worker slots are never parked waiting on quota.
- **Browser-pool admission cap:** the ~135-slot browser pool's admission control lives in the Pacer; combined with the browser task queue's 120s ScheduleToStartTimeout, saturation becomes backpressure the workflow observes and routes to handoff (AC-45).
- **Throttle-first on state loss:** Redis is deliberately non-durable for this purpose. On Redis loss, buckets re-initialize EMPTY — the system throttles first and refills on observed source responses, so a cache wipe can never produce an unthrottled burst against a source quota.
- **G2 per-app contingency (config flip, no re-architecture):** setting `quota_scope[meetup]=app` collapses Meetup pacing to ONE global 500pt/60s bucket with deficit-round-robin over per-user FIFO lanes (~33 RSVP chains/min at the ~15 pts/chain estimate — a figure the G2 spike must replace with a measurement). When a lane's projected queue wait exceeds **5 minutes**, the Pacer signals DEGRADE and the registration workflow routes that lane to handoff; the autonomous-Meetup SLA declaration flips to **none** — exactly the FR-10.4/G2 posture of global fair share plus handoff with no fixed SLA number.

Launch parameters: Meetup 500pt/60s per token (per app under the flip); Luma 100 POST/5 min per calendar, 429 ⇒ 1-min full block; Ticketmaster 5 rps + 5,000/day per app key; browser pool cap ~135; DEGRADE threshold 5 min projected wait; estimated per-app throughput ~33 chains/min pending G2 measurement.

## Consequences

**Easier:**
- The hot path stays fast: lease acquisition is a sub-millisecond Redis operation instead of an engine Update, and there is no per-call Temporal action cost for pacing.
- The G2 downside is pre-absorbed. If Meetup's quota is per-app, operations flip one config key; fairness, queueing, degrade-to-handoff, and the SLA retraction are already wired. No design change stands between the spike result and production posture.
- One component owns all source politeness plus browser-pool admission, giving a single place to instrument quota consumption, `retry-after` compliance (AC-73), and saturation backpressure (AC-45).
- Compliance posture is structural: pacing enforces the FR-7.6 human-cadence obligation at the only chokepoint every wire call passes through.

**Harder / risks accepted:**
- A second stateful substrate (Redis) sits beside Temporal and Postgres; its failure mode is deliberately availability-degrading (throttle-first), which trades user-visible slowdown for quota safety. The empty-re-initialization and `resetAt`-honoring behavior must be fault-injection-tested so a Redis loss provably cannot emit an unthrottled burst — this is a committed test, not an assumption.
- Pacer state is advisory, not journaled: a projection returned to a workflow can go stale before the durable timer fires; the design accepts re-acquire-on-wake rather than reserved leases.
- Under the per-app flip, the global Meetup ceiling (~33 chains/min at the unverified ~15 pts/chain estimate) is already roughly 6× oversubscribed against the 100k-user demand arithmetic — the degraded mode is honest about being a fair-share lottery feeding handoff, and handoff-lane staffing (O-9, G1) inherits the overflow.
- The 5-minute DEGRADE threshold sets the trade between lottery patience and handoff volume with no measurement behind it yet.

**Follow-ups committed:**
- Run the G2 spike before declaring any autonomous-lane SLA (NFR-2 deferral): measure real `consumedPoints` per RSVP chain and the actual quota scoping.
- Fault-injection test for Redis loss (empty re-init, no burst) and an AC-73 fixture proving Meetup points / Luma POST / TM rps budgets and `retry-after` are honored.
- Feed G1 request-mix results into browser-pool sizing (~135 is provisional) and handoff staffing.

**Owner-ratification riders:**
- OWNER RATIFY: the 5-minute DEGRADE threshold (projected queue wait before a degraded Meetup lane routes to handoff) — it sets the fair-share-lottery vs handoff-volume trade in per-app mode.
- The G2 spike remains DESIGN-BLOCKING for any autonomous-lane SLA number: it must measure real `consumedPoints` per RSVP chain (the ~15 pts figure is an unpublished estimate) and per-token vs per-app quota scoping before an SLA is declared.
