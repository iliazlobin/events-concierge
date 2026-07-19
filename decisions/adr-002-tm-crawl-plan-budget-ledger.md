# ADR-002: Ticketmaster crawl plan and budget governance (resolves O-8)

**Status:** accepted — with owner-ratification riders (see Consequences) · **Date:** 2026-07-10

## Context

Ticketmaster is the richest catalog source and hands the system exactly one shared app key with a hard 5,000-calls/day budget, a 5 rps cap, and a `size × page < 1000` deep-paging cap (FR-3.4, `d11`/`d15`); a naive per-request fan-out exhausts that key at roughly 4k users, which is why FR-3.3 mandates a scheduled, geo/category-sharded read-through catalog crawl serving all tenant reads from the local store. O-8 left the crawl schedule itself open — shard grid, cadence, and budget accounting versus the freshness SLA — and this ADR closes it. The binding forces:

- **NFR-1:** a newly published event discoverable within ≤6h of crawl cadence; interactive p95 ≤5s served from the cache — the crawl is launch architecture, not optimization.
- **NFR-17 / FR-8.7a:** organizer cancel/reschedule for every `registered`/`scheduled` TM event detected within ≤6h; Ticketmaster's declared modality is catalog-crawl delta plus scheduled re-poll (verified by AC-60).
- **FR-3.4 / AC-20:** no request may reach `size × page ≥ 1000` or exceed 5 rps — log-asserted in CI.
- **AC-21:** a second tenant requesting the same city/date must add zero live TM calls.
- **FR-10.4 / AC-73:** the TM rate budget sits behind the fair-share scheduler and honors `retry-after`.
- **FR-3.2 / FR-3.6:** Eventbrite (and practically Luma) discovery arrives only through the SerpApi `google_events` funnel, so the funnel's own daily search budget is part of the same governance question.
- Re-fetching is the one unaffordable operation: the daily budget, not the 5 rps cap, is the binding constraint (500 calls at 2 rps is ~4 minutes of wall clock per cycle).

## Options considered

1. **Thin 14-day-horizon crawl (~1,200 calls/day) with query-time federation for everything else.** Minimizes standing quota spend and leaves ample metro headroom (~112 metros) — but both numbers are artifacts of a truncated horizon: an event published for next month is not discoverable within 6h, it is not discoverable at all until T−14d, a direct NFR-1 violation for far-out publications that the proposal never flagged as a trade. It also parks SerpApi's 1–3s+ latency variance on the interactive path, against FR-3.3 and the p95 budget. Rejected.

2. **Newest-sorted delta crawl (100 metro×segment shards, size=200/max-5-pages, 4 delta cycles/day + 1 daily lookahead backfill; worst-case ~2,500 calls/day against a 4,750 soft cap).** Its budget ledger and reserve concept are sound and are adopted, but as the sole NFR-1 mechanism the delta crawl depends on unverified TM `sort=newest` semantics and leaves modified-far-out-event detection to a daily backfill — a 24h worst case against a 6h target. Its "20-metro stated owner assumption" anchoring the arithmetic is invented; no signed decision pins a metro count. Kept only as a post-launch budget-optimization spike.

3. **Uniform 50-metro grid at 6h cadence (~3,200 calls/day, 64% of budget).** Correct shape, but the footprint consumes headroom that should fund the re-poll reserve and retry/split slack, and — like option 2 — the metro count rests on no ratified decision. Rejected at that size in favor of the same grid halved, with the metro count surfaced as an explicit owner-ratification item rather than an assumed constant.

4. **Budget enforcement by monitoring/alerting instead of ledger accounting.** Rejected: an alert fires after the spend; only a decrement that strictly precedes dispatch makes breaching 5,000/day arithmetically unreachable rather than operationally unlikely.

## Decision

We will run the Ticketmaster catalog crawl as a Temporal-scheduled facet sweep over cells = (metro, segment, date-window): **25 metros × 5 TM segments × 2 date windows {0–14d, 15–90d} = 250 cells**, both windows at 6h cadence — a 5h30m schedule plus ≤15min pipeline lag holds publication-to-discoverable ≤6h (NFR-1). At size=200 and ~2 pages per cell this is **~500 calls/cycle × 4 cycles/day ≈ 2,000 calls/day typical (~40% of budget)**, under a **soft cap of 4,500** with a **500-call reserve** for registered-event re-polls, retries, cell splits, and miss-fills.

We will govern the budget with a **BudgetLedger whose decrement strictly precedes every dispatch** — the 5,000/day limit is enforced by ledger accounting, not monitoring, so exceeding it is arithmetically impossible. A global 2 rps token bucket paces dispatch (500 calls ≈ 4 min wall-clock per cycle; the daily budget, never the 5 rps cap, binds).

We will make cap violations **unconstructible at the adapter**: the TM adapter accepts only size=200 with page index 0–4 (200×4=800 < 1,000) and sits behind the rps bucket, so no caller can issue a `size × page ≥ 1000` or >5 rps request (AC-20 structural, not caller discipline). A cell projected to exceed 1,000 items **recursively splits by genre, then by date-bisection — never deep-pages**; a cell still over the cap after all splits truncates at the API cap, logs a completeness metric, and relies on the SerpApi funnel and per-user discovery to backfill the tail. The crawler and the by-id reconcile re-poller are the only code paths linked against the TM client — no live-path client exists, so AC-21 holds by construction.

We will fund a **reconcile re-poller from the 500-call reserve**: every 6h, a by-id GET of every TM canonical event with an active `registered`/`scheduled` lifecycle — NFR-17 detection never depends on cell cadence (500/day supports ~125 concurrently watched events; overflow prioritizes soonest-starting and alerts).

We will apply a fixed **degrade ladder under budget pressure**: at 80% ledger consumption, demote the 15–90d window first to 12h cadence (≈55 calls/metro/day ⇒ ~81-metro ceiling), then to 24h (≈42.5 ⇒ ~105 metros) — **never the 0–14d window** users actually book, and never the reserve-funded re-poller. On TM 429, honor `retry-after`; on hard exhaustion, pause the crawler until midnight UTC while serving continues from the catalog unaffected.

We will run the **SerpApi sweeper on a scheduled cadence** (FR-3.6 — never request-time): 4 query templates × 25 metros × 4 cycles/day = **400 searches/day ≈ 12k/month on the $150/15k plan**, the sole Eventbrite discovery path (FR-3.2).

## Consequences

**Easier.** NFR-1 holds for both near and far date windows with 60% budget headroom; AC-20/AC-21 become construction-time properties verifiable in CI rather than runtime discipline; NFR-17 for booked events is decoupled from every future crawl-schedule change; budget exhaustion has a deterministic, pre-decided shed order instead of an on-call improvisation; typical spend (~40%) leaves room for retries, splits, and organic metro growth to ~40 metros at uniform 6h before the ladder engages.

**Harder / accepted risks.** Metro expansion is coupled to third-party discretion: past ~81 metros the far window drops to 12h, past ~105 to 24h, and beyond that only a case-by-case, compliance-gated TM quota raise helps. Ultra-dense cells that exceed 1,000 items after genre and date splits are truncated — a TM API property no topology fixes; the funnel and per-user discovery only partially backfill. The ~2-pages-per-cell figure (~1.5 hot / ~2.5 lookahead) is an estimate, so the 2,000/day arithmetic is provisional until probed. SerpApi is a single third-party proxy carrying all Eventbrite discovery; an outage bounds that source's staleness to the outage length with no in-scope fallback. The reserve sizing (~125 concurrent watched TM events) is a guess until real request-mix data lands.

**Follow-ups committed.** Build-time probe against the live Discovery API before launch (see riders); re-visit option 2's newest-sorted delta crawl as a budget-optimization spike post-launch; re-confirm reserve sizing once G1's request-mix measurement lands; wire ledger-consumption and cell-truncation completeness metrics into alerting.

**Owner-ratification riders:**

- **OWNER RATIFY: 25-metro launch grid.** No signed decision pins a metro count (the "20-metro owner assumption" circulating earlier was invented; O-8 pins nothing). Ratify 25 plus the expansion ladder: >~40 metros at uniform 6h approaches budget; 12h far-window carries ~81; 24h carries ~105; beyond that requires the quota raise.
- **OWNER RATIFY: the far-window-freshness degrade ladder as explicit product posture.** NFR-1's ≤6h applies to BOTH windows at launch; under pressure or metro expansion, far-out freshness (12–24h) is the accepted negotiating chip — a product decision, not an ops improvisation.
- **OWNER RATIFY: SerpApi spend** — 400 searches/day ≈ 12k/mo on the $150/15k plan, scaling linearly with metros and query templates.
- **OWNER ACTION: apply for the discretionary TM quota raise early** as a parallel track (case-by-case after ToS/branding review, `d03`/`d11`), accepting it may be refused — it is the only escape past the ~105-metro ceiling on the default key.
- **OWNER GATE: the build-time probe must validate the ~2-pages-per-cell assumption** (and genre/date-bisection split coverage) against the live Discovery API before the 6h-both-windows promise is confirmed.
