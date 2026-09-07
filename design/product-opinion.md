# Events Concierge — Product Opinion

**v1.1 · 2026-07-10 · status: DRAFT for owner review** *(v1.1 folds all 10 findings of an adversarial fidelity review against the dossiers, the signed-off requirements, and the 7 HARD scope decisions)*
**Basis:** product-landscape dossiers `research/18`–`25` + brief `research/00c` (this wave), technical corpus `research/01`–`17` + briefs `00`/`00b`, signed-off `design/requirements.md` v0.2, and the owner's 2025 prototype (github.com/iliazlobin/events-planner-agents). Claims cite dossiers (`d18`…); this document takes positions — the evidence record lives in the dossiers.

---

## 1. The opinion in one paragraph

Build the **action layer for real-world events**: a subscription concierge that holds a standing natural-language brief ("keep my weekday evenings interesting, nothing over 45 minutes away"), watches every source in the user's city, and converts intent into **confirmed, reconciled calendar entries** — autonomously where the source permits, one tap where it doesn't. Do not build an endless discovery destination, browse feed, social network, or better catalog. A bounded, user-initiated brief/control surface is part of the action layer: it may show 1–3 focused picks plus truthful request, plan, handoff, and preference state, but it must not optimize for scrolling or app opens. Discovery is commoditized (three ticketing giants already live inside ChatGPT; a weekend n8n template replicates NL-search); lists are a YC-named tarpit that killed a decade of startups; and every incumbent is structurally locked out of the back half of the loop by its own inventory economics. As of July 2026 **nobody — platform, horizontal agent, OSS project, or indie — connects discover → rank → register → calendar → reconcile** (`d18`–`d21`). That connected back half is the product, the moat, and the demo.

## 2. Why now (three clocks running)

1. **The fragmentation clock.** For the first time since ~2010 there is no default event-discovery surface: Facebook Events collapsed into API darkness, Meetup is decaying under Bending Spoons fee hikes, and supply scattered across Luma, Partiful, Eventbrite, Discord, IG bios, and indie calendars (`d23`). Cross-source is newly valuable precisely because no single destination can claim coverage anymore.
2. **The capability/trust clock.** Agents are finally plausibly good enough for the terminal RSVP click — simple live-web actions at 75–90% happy-path in 2026 (judge-caveated leaderboard numbers), up from 56–64% in 2025, while the transactional slice under real-world faults still runs below 50% worst-case, which is exactly why the honest split + handoff lane exists (`d22`). Meanwhile every horizontal agent still *gates or refuses* exactly that step (ChatGPT pauses on logins/payments; Gemini confirms before form submits; Claude-for-Chrome advises against credentialed transactions — `d19`). Meanwhile consumers normalized bounded delegation (74% would delegate routine tasks; Blockit sells agents that autonomously commit calendar time — `d18`, `d24`). The window where a vertical player can own "actually finishes the job" before the horizontals relax their gates is open but not permanent.
3. **The category clock.** "AI event concierge" has no consumer owner, but Posh raised $37M in March 2026 explicitly against "what are we doing tonight?" with agentic framing, Partiful shipped ticketing, Eventbrite is adding AI feeds (`d18`). The name and the quadrant are claimable today; probably not in 12 months.

## 3. Positioning

**Identity:** *"One brief. Every source. Your calendar stays true."* The only service that searches everything and actually gets you in — then keeps your calendar honest when organizers cancel, reschedule, or you bail.

- **Position against horizontal agents, not event platforms.** The quotable contrast: a general agent runs 5–30 supervised minutes, ~40 runs a month, and stops to ask at every login; the concierge acts in seconds, unattended, continuously, and reconciles afterward (`d19`). Never pitch "better recommendations than Luma" — pitch "the part after the recommendation, everywhere."
- **Market the honest split as the industry-validated pattern, not a limitation.** OpenAI killed in-chat Instant Checkout after only ~30 merchants ever went live and Walmart's in-chat conversion ran ~1/3 of its own site's; StubHub/Ticketmaster/SeatGeek all chose discovery-in-chat + merchant-owned checkout; travel's leaders ship recommend-then-handoff as their *only* lane (`d19`, `d24`). "The concierge does 95%, you tap once" is what the market's biggest players converged on after burning money on full autonomy.
- **Incentive-clean ranking is a named differentiator.** Every incumbent monetizes inventory and steers its feed accordingly (Fever's engine pushes Fever Originals). A user-paid agent is the only actor in the landscape whose ranking answers to the user alone — say so explicitly (`d18`).
- **Safety as a categorical claim.** A hard-allowlisted action space — RSVP endpoints + calendar writes only; no email-send or payments in the action space *at launch* (the deferred payment-port seam of decision 4 stays intact), and navigation allowlisted within the registration lane (read-only browser discovery per decision 2 is unaffected) — turns the browser-agent prompt-injection epidemic into our marketing: a claim no horizontal agent can make (`d19`).
- **North-star metric: confirmed-and-attended events per user per month.** Explicitly not DAU/sessions — the graveyard's fatal metric (IRL faked 95% of its MAU rather than admit browse-retention was fiction) (`d18`, `d23`).

## 4. The wedge user

**The 25–40 urban "socially displaced" adult** — new-in-town movers, remote workers (34% of new friendship-app users), post-friend-group millennials. They have money (the $30 ticket sweet spot plus a service-layer budget), stated intent (79% of 18–35s plan more events in 2026), acute decision fatigue (51% want logistics fully handled), and no local discovery graph (`d25`). They live across Meetup + Luma + Eventbrite in one metro — the exact fragmentation we aggregate, and the persona whose Meetup group memberships light up our only on-SLA autonomous lane (group affiliation is also the empirically strongest attendance-prediction signal — ranking confidence and actuation permission coincide, `d22`).

**Not Gen Z first.** They discover on TikTok, enjoy the hunt, and expect free (`d25`). **Not music/nightlife first.** DICE/RA/Posh own it and it is paid-ticket-heavy, outside free-RSVP scope (`d18`).

**Framing: decision fatigue, not FOMO.** Present 1–3 confident picks per ask, never a catalog — The Nudge ships exactly 3 plans/week to 1M+ subscribers; browse feeds are the anti-pattern (`d25`). Post-RSVP messaging should read like a personal invitation ("I got you in Friday 7pm — it's on your calendar"), because personal invitations, not browsing, drive attendance (`d21`).

## 5. What this product is NOT (anti-scope)

- **Not a platform or social network.** Platform-side event networks die of two-sided supply cold-start regardless of backing (freeCodeCamp archived Chapter at 1.9k stars; 13+ of ~40 catalogued Meetup clones dead — `d20`). We aggregate supply that already exists — no cold start.
- **Not a ticketer, not paid checkout.** Songkick crossing from discovery into ticketing entered Live Nation's kill zone; the BOTS-Act/SCA/chargeback analysis already deferred paid scope (decision 4). The graveyard vindicates it (`d23`).
- **Not an endless destination app.** Calendar and the intended push digest remain proactive surfaces; a bounded brief/control surface may show 1–3 request-specific picks, plans, handoffs, and settings. It must not become an infinite catalog/feed or justify work through app-open metrics (`d23`).
- **Not an inventory subsidizer.** Jukely proved ~$25/mo events WTP exists, then died eating ticket-cost risk. We price labor, never inventory (`d23`, `d25`).
- **Not a human-ops concierge.** Every human-labor concierge at consumer prices died or fled (Yohana $249/mo — dead; Fin, Magic, Clara's human tier). The only affordable human in the loop is the user's own tap (`d24`).
- **Not a stealth scraper.** Amazon v. Perplexity (preliminary, stayed, ruling pending) says user permission ≠ site authorization for disguised automation; Cloudflare default-blocks anonymous agent traffic from Sept 15, 2026. We act as the authenticated user, never spoof, honor blocks — and pursue signed-agent (Web Bot Auth) registration, which converts the industry's bot wall into a whitelist moat for a registered, well-behaved vertical agent (`d19`).

## 6. Business shape

**Pricing (hypothesis to validate, P2 below):**

| Tier | Price | Contents | Anchor |
|---|---|---|---|
| Free | $0 | Weekly taste-tuned digest (1–3 picks/slot), one-tap handoff links, paste-a-link capture *(pending D9 ruling)* | curation-only caps at ~$6–9/mo (The Nudge); digest is the habit builder, never the paid feature (`d25`) |
| Concierge | **$15–25/mo** | Autonomous RSVP (permitted lanes), RSVP sniping, attendance loop, cross-source reconcile, standing briefs | between Skej ($10–15) and Howie ($25–95); inside the proven 222/Timeleft/Campnab band; straddling the $20 ChatGPT Plus anchor (`d24`, `d25`) |
| (Later) Assured | $50+/mo | Guaranteed-spot service for scarce drops, possibly per-success fees ($5–15) | Dorsia/Appointment Trader show premium money buys *access*, not recommendations (`d24`, `d25`) |

Flat pricing, published limits — never Lindy-style opaque credits (`d19`). No ads, no organizer fees, no affiliates at launch: user-paid is the neutrality moat (`d18`).

**Unit economics sanity check.** The technical corpus prices a confirmed RSVP at ~$0.05–0.12 (API lane) / ~$0.15–0.40 (browser lane), blended ≤$0.20 (`00b`, NFR-5). A $20/mo subscriber consuming even 15 confirmed RSVPs costs ~$3 in variable action cost plus amortized crawl/rank — healthy software margins, with the browser lane capped by policy budget. The margin story only breaks if browser-heavy usage dominates, which the API-first posture and per-lane cost SLOs already guard (NFR-5). What the corpus does *not* yet establish is tenure: the wedge need is plausibly self-extinguishing (a successfully-connected user needs us less), and no analog's churn is known — the sharpest open economics question (P6).

**GTM.** One metro — **NYC** — at full depth before any second city; multi-city shallow is what actually killed YPlan/Sosh/Jukely, and capped even the beloved WillCall at 3 cities (`d23`). Depth includes the non-ticketed long tail (trivia nights, gallery openings, DIY shows) via the curated-human layer (The Skint, nonsense nyc, 19hz-class calendars) ingested **with attribution** — these communities are anti-scrape-and-resell, and etiquette there is a distribution asset (`d21`). Distribute through scenes (Luma calendar-follow graphs, Meetup groups, city subreddits, local newsletters), ride the loneliness/third-places narrative (front-page Axios, July 5 2026), and skip the "things to do in X" SERP that Fever's 60M-uniques media network owns (`d25`). A per-user private **ICS feed** of concierge picks is a zero-partnership distribution channel that works in every calendar client on day one (`d20`).

## 7. The moat, stated honestly

Benchmark accuracy is closing fast — do **not** build the strategy on horizontal-agent incompetence (`d19`, `d22`). What survives when they get good:

1. **Statefulness.** Standing briefs, a persistent taste model, attendance ground truth from the reconcile loop — behavior-grounded taste data no browse-only incumbent can generate (the Zest pattern), and the very asset acquirers paid for in the Songkick/Suno and Fever/DICE deals (`d18`).
2. **Economics.** Continuous background discovery + reconciliation is structurally incompatible with per-message-capped horizontal agents, and free/community events carry no fee pool to attract platform aggregators (`d19`).
3. **Permitted access.** Per-user authenticated connectors (harder to lock out than central scraping), Meetup-member API RSVP, signed-agent registration — the compliant lane through the 2026 bot walls (`d19`, `d23`).
4. **The reconcile loop itself.** Stateful, fee-less, per-source drudgery — cancel detection, un-RSVP, calendar truth — that is structurally unattractive to every player surveyed, and the single feature that exists *nowhere* (cross-source) today (`d18`, `d24`).

## 8. Autonomy posture — reconciling the evidence with decision 3

The landscape's clearest trust finding: 74% delegate routine tasks / 32% accept delegation within parameters / 9% accept autonomous payment; six in ten UK consumers say (stated intent, about AI *shopping* agents — extrapolated here) they would stop using an agent after one visible mistake; gentle-default products (Reclaim) retain where aggressive ones (Motion) generate complaints; Lindy's confirm-first-then-promote is the productized pattern (`d24`).

Decision 3 (HARD): fully autonomous within the permitted surface; guardrails are **configured policy, not interactive prompts**. The evidence does not overturn this — it refines what the *default policy configuration* should be. Recommended reconciliation, which keeps decision 3's architecture intact:

- The autonomy level is a **per-lane policy dial the user owns**: `digest-approve` (default at onboarding) → `auto-RSVP` (user promotes a lane after seeing the concierge be right, or immediately at setup). No per-action confirmation gates exist anywhere in the architecture — exactly as decided; "approve" in digest mode is the same one-tap handoff surface FR-6 already specifies.
- **Auto-demotion on risk** (new source, first event of a category, any fee surface) is a policy rule, consistent with FR-5.9/5.10.
- This also matches how we earn the right to full autonomy: preview-before-commit receipts, visible undo (un-RSVP), and a mistake-rate SLO make the promotion offer credible (`d24`).

This is D10 in the delta list; it needs an explicit owner ruling because it touches a HARD decision's *default*, not its architecture.

## 9. Proposed requirement deltas (for owner sign-off, → requirements v0.3)

The signed-off v0.2 covers the loop, honest-split routing, handoff, policy, and organizer-side reconcile. The landscape adds ten deltas — full statements in `00c`; priorities mine:

| # | Delta | Verdict rationale |
|---|---|---|
| **D1** | **Attendance loop / cancel hygiene** — T-24h confirm nudge, auto-un-RSVP on decline/no-response/conflict, concurrent-open-RSVP cap, per-user attendance score throttling autonomy | **Highest priority.** Free-RSVP no-shows run 40–60%; bots no-show 4× and six states are legislating in the adjacent space; organizers ban serial flakes. Simultaneously regulatory shield, organizer-trust moat, and a real feature ("never be the flake"). v0.2 handles organizer-side reconcile but not user-side intent decay (`d24`, `d25`) |
| **D2** | **RSVP sniping** — watch trusted groups/venues, instant RSVP on open, waitlist-promotion detection | 15 years / ~28 independent DIY Meetup bots prove the demand ("full after 3 minutes"); it lives exactly in our permitted-autonomy tier; the clearest chargeable moment in free-RSVP scope (`d20`, `d21`) |
| **D3** | **Taste cold-start onboarding** — 60-second taste interview, Meetup-group import, streaming-library ingest, Qloo buy-vs-build | Cold start is solvable by elicitation, not waiting for behavior; music-sync is the proven pattern (DICE/RA) (`d18`, `d22`) |
| **D4** | **Social/safety signals in ranking rationale** — attendee composition, friends/community signals; "why I picked this" includes who will be there | Meetup made gender/age mix first-class for exactly the "should I go?" decision; auto-RSVP trust stalls without it, especially for women users (`d18`) |
| **D5** | **Weekly digest as primary proactive surface** — voice/personality, one-tap actions, push (email/SMS) | Push + trusted voice is what survives decades (Skint/Funcheap); personality measurably drives digest engagement; elevates FR-6 from contract to product surface (`d21`, `d25`) |
| **D6** | **Independent out-of-band RSVP verification** before any calendar write (confirmation-email parse / page re-check / API read-back) | 68% of agent booking failures are silent false successes — the agent's own success report is the least trustworthy signal in the pipeline (`d22`) |
| **D7** | **Agent-identity policy + signed-agent registration** — never spoof, honor blocks, prefer user-session execution, pursue Cloudflare Web Bot Auth | Amazon v. Perplexity + Sept 15, 2026 default agent-blocking make stealth automation injunction-grade risk and a closing door; registration converts the wall into a moat (`d19`) |
| **D8** | **Supply-density preflight + honest-failure UX; supply-risk register** (Bending Spoons = risk #1; no source >40% of a metro; connector deprecation playbooks) | Thin-result weeks are trust death; the scrape war is the one graveyard failure mode our design does NOT neutralize (`d21`, `d23`) |
| **D9** | **Growth/coverage bridges** — forwardable invite cards with one-tap RSVP; paste-a-link ingestion (TikTok/IG/Partiful URL → extract → calendar) | Honestly bridges the permanent private-graph blind spot; doubles as the growth loop in a category where every incumbent grew via invites (`d25`) |
| **D10** | **Graduated-autonomy default** (per-lane digest-approve → promote), auto-demotion on risk | See §8 — needs an explicit owner ruling against decision 3's default (`d24`) |

Also fold from `d20`/`d21` into existing FRs: a freshness/staleness subsystem with per-source last-good-scrape SLAs and event "uncertainty" scoring (extends NFR-1/FR-3); a standing anti-bot budget line (residential-proxy/Bright Data-class) for browser-tier sources; fuzzy cross-source entity resolution is already FR-3.8 — treat 206.events' dedup/uncertainty caches as prior art.

## 10. Risks the opinion accepts, and their tripwires

1. **Bending Spoons (Meetup + Eventbrite landlord).** Fee-hike/paywall/API-monetization playbook; Meetup is our only on-SLA lane. *Posture:* treat the Meetup connector as a depreciating asset; per-user OAuth (ban surface scoped to the user; quota scoping unknown pending G2 — fair-share-queue contingency if per-app); degrade-to-handoff never breaks the product (the honest split means autonomous-lane loss ≠ product loss). *Tripwire:* Meetup API pricing/ToS change → G2 spike rerun + war-game P3.
2. **Incumbent copy or block.** Most probable response to visibility is BLOCK, not copy (a platform concierge would only cover its own inventory anyway). *Tripwire list (quarterly):* Meetup/Luma app in ChatGPT's directory; Gemini relaxing its form-submit gate; ChatGPT recurring tasks / cap raises; Ninth Circuit ruling either way (`d19`).
3. **No-show amplification.** An auto-RSVP agent without D1 is the villain six legislatures are writing laws about. D1 is non-negotiable; "concierge users show up" should become an organizer-facing quality claim (`d24`, `d25`).
4. **Thin autonomous surface.** The on-SLA lane (Meetup member groups) may be a small share of a real user's weekly candidates — if so the felt product is digest + handoff, which prices at $5–9, not $20. This is the single sharpest threat to the pricing opinion → P1 census before locking pricing.
5. **Self-extinguishing need / unknown tenure.** → P6 teardown; if tenure proves short, add a durable second job (couples/friend-group planning — group event rec is a mature, unproductized academic subfield, `d22`) to the roadmap.

## 11. What to validate next (the P-gates — from the completeness critic)

The landscape research is adequate for a durable opinion; what remains is validation, not more web research. In priority order:

- **P1 — NYC supply census (1–2 wk).** Crawl one week of NYC inventory across all sources + curated long tail; tag every event by RSVP-actionability tier and taste cluster; simulate 10 wedge-persona briefs → candidates-per-request and the **permitted-auto share**. Sets the metro-2 density bar; makes or breaks the pricing opinion. (Subsumes requirements gate G1's request-mix estimation.)
- **P2 — WTP smoke test (2–3 wk, parallel).** 15–25 problem interviews with the wedge persona + a priced landing/waitlist test at $15/$25; ideally a 10-user manual-concierge MVP at $20/mo. Nobody has ever charged for labor over free events — the ladder is analogical until this runs.
- **P3 — Incumbent war-game memo (2–3 days, desk).** Block/Copy/Partner scenarios × {Bending Spoons, Luma, Partiful, Posh, Apple, Google}, with time-to-response, surviving product value per cell, and pre-commitments. Decides how loudly to claim the category name.
- **P4 — Credential-grant willingness (inside P2).** Which OAuth/account grants the persona will actually give, in what order, at what tier; produce a degraded-mode matrix (calendar-only / no-platform-account) so requirements specify what works at each consent level.
- **P5 — Private-graph share (inside P2).** Have interviewees log a month of events by source (public vs private invite/chat); >40% private promotes D9 from bridge to core.
- **P6 — Tenure/CAC teardown (desk).** Timeleft/Nudge/222/BFF channel spend and review-cohort churn language; LTV:CAC under 6/12/18-month tenure scenarios.
- **P7 — Bottom-up TAM (1 page).** Wedge-persona count per top-10 metro × analog penetration × P2 conversion → is this a $1–5M ARR prosumer niche or venture-scale? Sets the ambition level of everything above.

Plus the pre-existing technical gates: **G2** (Meetup `createEventRsvp` spike — design-blocking) and **G3** (RelayInbox acceptance) from requirements §8.

## 12. Questions for the owner (with recommendations)

1. **Autonomy default (D10):** adopt per-lane digest-approve default with promotion, full-auto opt-in at setup? **Recommend: yes** — keeps decision 3's architecture, follows the trust evidence.
2. **Pricing frame:** free digest + $15–25/mo concierge tier, flat, no credits? **Recommend: yes, pending P1/P2** — and hold the sniping per-success fee for the later Assured tier.
3. **Launch metro NYC** with a numeric density bar gating metro 2 (set from P1)? **Recommend: yes.**
4. **Category naming:** move on "AI event concierge" branding now vs after P3's war-game? **Recommend: run P3 first (days, not weeks)** — visibility accelerates the block response and the name is not yet contested.
5. **Requirement deltas D1–D9** into a requirements v0.3 revision now, or after P1/P2? **Recommend: fold D1–D8 now** (they're evidence-stable), hold D9's scope and D10 for the answers above.

---

*Artifacts map: dossiers `research/18-25` (landscape evidence) · `research/00c` (landscape brief) · this opinion (positions + plan). Next phase after owner review: requirements v0.3 fold-in, then relaunch the design judge panels (the 2026-07-02 panels died with their session).*
