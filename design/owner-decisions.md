# Events Concierge — Owner Decision Brief

**Date:** 2026-07-14 · **Purpose:** clear the design-phase review gate. Everything below is waiting on you.
**Basis:** the 11 ADRs (`decisions/`), the four judge-panel verdicts (`design/panels/`), and the product opinion (`design/product-opinion.md` §12). The design doc (`design/system-design.md`) passed the rubric gate; nothing here reopens a design decision — these are the launch parameters, posture calls, and scope rulings the design deliberately left to you.

## How to use this

Three parts: **A — five strategic rulings** (product shape; the biggest calls), **B — 37 technical ratification riders** (grouped; most are "accept the designed value", a few are real forks flagged **FORK**), **C — the empirical gates** that block build regardless of the above. Each item has a **Recommendation** and a checkbox. Checking the recommendation is the fast path; anything you change, I fold into requirements v0.3 and the affected ADR (as a superseding ADR, since accepted ADRs are immutable).

Legend: **[accept]** = ratify the value the design already uses (low stakes). **FORK** = a genuine either/or that changes behavior. **[action]** = something to start now (a procurement or application lead time). **[gate]** = blocks build until an empirical result lands.

---

## Part A — Five strategic rulings (from the product opinion §12)

- [ ] **A1. Autonomy default (D10).** Adopt a per-lane autonomy dial defaulting to `digest-approve` at onboarding, with the user promoting a lane to `auto-RSVP` after seeing the concierge be right (or opting into full-auto at setup). No per-action confirmation prompts exist anywhere — "approve" in digest mode is the same one-tap handoff surface FR-6 already specifies. **FORK:** this softens the *default* of HARD scope decision 3 (fully autonomous) without changing its architecture. **Recommendation: yes** — it matches the trust evidence (74% delegate routine tasks / 32% within parameters / 9% autonomous payment) and keeps decision 3 intact. If yes, D10 folds into requirements v0.3.

- [ ] **A2. Pricing frame.** Free taste-tuned digest + a **$15–25/mo** concierge tier, flat pricing with published limits, no opaque credits, no ads/organizer-fees/affiliates at launch. **Recommendation: yes, pending P1/P2** — hold the RSVP-sniping per-success fee for a later "Assured" tier. This is a product/GTM ruling, not a build input; it does not block design.

- [ ] **A3. Launch metro = NYC, single-metro-deep,** with a numeric supply-density bar (set by P1) gating any second metro. **Recommendation: yes** — multi-city-shallow is the named killer of YPlan/Sosh/Jukely. This couples to riders B1/B2 (the 25-metro *crawl* grid is a capacity-sizing number, not a go-to-market claim; NYC is the launch market).

- [ ] **A4. Category naming.** Move on "AI event concierge" branding now, or after the P3 incumbent war-game? **Recommendation: run P3 first** (it is days, not weeks) — visibility accelerates the incumbent block response and the name is not yet contested, so a short delay is cheap insurance.

- [ ] **A5. Requirement deltas into v0.3.** Fold the evidence-stable deltas **D1–D8** now, hold **D9** (growth/invite bridges — scope depends on P5 private-graph share) and **D10** (A1 above) for your ruling. **Recommendation: fold D1–D8 now** — D1 (attendance loop / cancel hygiene) is the highest-priority delta (free-RSVP no-shows run 40–60%; six legislatures are writing bot-no-show law) and is a regulatory shield, not a feature nicety. This is thread 2 of this session; on your yes I draft v0.3 with D1–D8 + the FR-2.7 fidelity fix.

---

## Part B — 37 technical ratification riders

### B-I. Launch scale, crawl budget & freshness (ADR-001, ADR-002 — 10 riders)

- [ ] **B1. [accept] 25-metro crawl grid + expansion ladder.** No signed decision pins a metro count; the design sizes the Ticketmaster crawl for 25 metros (~2,000 of 5,000 daily calls). Expansion ladder: >~40 metros at uniform 6h approaches budget; demote the far (15–90 d) window to 12h ⇒ ~81 metros, 24h ⇒ ~105; beyond ~105 needs a TM quota raise. **Recommendation: ratify 25** (comfortably covers an NYC-first launch with headroom).
- [ ] **B2. FORK — far-window freshness as the degrade chip.** NFR-1's ≤6h discoverability applies to *both* date windows at launch; under budget pressure or metro expansion, far-out-event freshness (12–24h) is the sacrifice, never the 0–14 d window users actually book. **Recommendation: accept** — a deliberate product posture (near-term events stay fresh; a concert 60 days out tolerating 12h staleness is invisible to users). Say no only if far-out freshness is a product promise.
- [ ] **B3. [accept] Per-user Meetup events are NOT promoted to the shared catalog at launch.** Cross-tenant Meetup catalog benefit is sacrificed to keep tenant isolation structural; the SerpApi funnel partly compensates. Flipping it post-launch requires both an adversarial visibility-classification fixture and a Meetup ToS/commercial-use review of redistributing token-fetched data. **Recommendation: ratify the deferral.**
- [ ] **B4. [accept] SerpApi spend** ≈ 400 searches/day ≈ 12k/mo on the $150/15k plan (scales linearly with metros/templates). **Recommendation: ratify.**
- [ ] **B5. [accept] pgvector re-benchmark before ≥5M rows.** The single-cluster/single-index posture is validated for 150–500k rows; a 10× corpus growth must be re-benchmarked before it is assumed safe. **Recommendation: ratify as a standing guardrail.**
- [ ] **B6. [action] Apply for the Ticketmaster quota raise early,** as a parallel track (case-by-case after ToS/branding review), accepting it may be refused — it is the only escape past the ~105-metro ceiling on the default key. **Recommendation: start the application now** (long, discretionary lead time; costs nothing to have in flight).
- [ ] **B7. [gate] Build-time probe must validate the ~2-pages-per-cell crawl assumption** (and genre/date-bisection split coverage) against the live Discovery API before the 6h-both-windows promise is confirmed. **Recommendation: schedule with the G2 spike** (both are live-API probes).

### B-II. Orchestration & policy (ADR-003, ADR-004, ADR-005 — 7 riders)

- [ ] **B8. [accept] FR-8.2 interpretation.** Discover/rank/select run as journaled activities in the *parent* request-workflow (the per-event child workflowId cannot exist until discovery mints `canonical_event_id`). Recorded as the closest achievable reading of FR-8.2, not an exception. **Recommendation: ratify** (structurally forced; no alternative exists).
- [ ] **B9. FORK — re-request of a previously failed (user,event).** Default: a handoff task with a deep link (strict FR-8.1 reject-duplicate). Alternative: `ALLOW_DUPLICATE_FAILED_ONLY`, permitting one autonomous re-attempt — deviates from FR-8.1's letter. **Recommendation: keep the handoff default** (simpler, safe); revisit only if data shows failed events are commonly re-registerable autonomously.
- [ ] **B10. [accept] Confirmation-wait timeout = 24h** before a pending registration routes to handoff. **Recommendation: ratify** (the mechanism is fixed by FR-8.4; 24h is a reasonable default).
- [ ] **B11. [accept] Child directive wait = 10 min,** defaulting to `close` on expiry. **Recommendation: ratify** (internal timing, low stakes).
- [ ] **B12. FORK — kill-switch grace window = 30 min** freeze-before-handoff (60s re-check). Shorter drains to handoff faster; longer preserves more in-flight work across brief engagements. **Recommendation: 30 min** — balances a brief accidental engage against not stranding in-flight registrations. A safety-conscious owner may prefer shorter.
- [ ] **B13. [accept] Meetup DEGRADE threshold = 5 min** projected queue wait before a degraded (per-app-quota) Meetup lane routes to handoff. **Recommendation: ratify,** revisit after the G2 spike measures real quota scoping.
- [ ] **B14. [gate] G2 spike stays DESIGN-BLOCKING for any autonomous-lane SLA number.** It must measure real `consumedPoints` per RSVP chain (the ~15 pts estimate is unpublished) and per-token vs per-app quota scoping before an SLA is declared. **Recommendation: acknowledge** — no autonomous-lane SLA is promised until this lands (thread 3 builds the harness).

### B-III. Browser-worker security posture (ADR-006 — 6 riders, the highest-stakes cluster)

- [ ] **B15. FORK — AC-13 kernel-separation by vendor attestation at launch.** The per-session kernel-isolation half of AC-13 is met by Browserbase attestation (SOC 2, contractual per-session Firecracker isolation) plus our behavioral egress tests, becoming fully first-party only at the self-host migration. This is the design's **one explicit binding-constraint exception**. **Recommendation: accept for launch** — self-hosting a hypervisor fleet for ~13 launch sessions breaches the cost bound and diverts a pre-PMF team; the exception is bounded and time-boxed.
- [ ] **B16. FORK — fill-time plaintext transits the vendor CDP gateway** (inside TLS) until self-host migration — an exposure an in-house broker would not have, bounded to per-fill scope of mostly short-TTL revocable cookies, nothing persisted vendor-side. **Recommendation: accept as the launch trade,** re-examined at the tripwire. This is the sharpest security concession; say no only if any credential transit through a vendor is disqualifying (which forces day-one self-host and its cost/timeline hit).
- [ ] **B17. [accept] Self-host tripwire = sustained 70 concurrent sessions** (~70% of the 100-session Startup tier), triggering both the Scale-250+ contract motion and the gVisor readiness spike. **Recommendation: ratify.**
- [ ] **B18. FORK — AC-13 "kernel" interpretation.** If a gVisor userspace kernel satisfies "no cross-tenant kernel sharing," the self-host migration uses gVisor on standard EC2. If hardware virtualization per session is required, it swaps to `runsc -platform=kvm` at roughly double the host count/cost. **Recommendation: accept gVisor as sufficient** (rule now so migration sizing is conscious); choose KVM only if your threat model demands hardware isolation.
- [ ] **B19. [action] Browserbase Scale contract checklist to lock before ~75k users:** ≥250 concurrency, 150/min creation, ZDR/data-handling, contractual per-session isolation, proxy-enforcement guarantee, persistence-off guarantee — a regression on any item fires the self-host trigger early. **Recommendation: ratify the checklist** as the procurement gate.
- [ ] **B20. FORK — opt out of Anthropic's computer-use injection classifier.** Its flagged-action human-confirmation step breaks the no-per-action-confirmation scope; the design substitutes the CaMeL Reader/Planner split plus deterministic action gates as the structural control. **Recommendation: opt out and rely on the structural control** — the classifier's confirmation prompt is incompatible with autonomous operation, and the CaMeL split is a stronger, non-interactive defense.

### B-IV. Lifecycle & change detection (ADR-007, ADR-008 — 6 riders)

- [ ] **B21. FORK — conflict-at-completion still writes the calendar.** When a handoff completes and `freeBusy` finds a conflict acquired since routing, the design surfaces a warning AND still advances to `registered` and writes the entry (the user factually holds the registration). **Recommendation: accept (warn-and-advance)** — the registration is real; blocking the calendar write would hide a booking the user made. If you want conflict to BLOCK the write, it is a one-activity change — settle now.
- [ ] **B22. [accept] Reopen-after-terminal** uses attempt-suffixed workflowIds `{user}:{event}:r{n}`; a closed history is never mutated. **Recommendation: ratify** (mechanism clarification of FR-8.1).
- [ ] **B23. [accept] Handoff TTL = min(7 days, event start),** reminders at T+24h and T+5d. **Recommendation: ratify;** these are O-9 tuning knobs, revisit with handoff-lane staffing data.
- [ ] **B24. [accept] Detection poll cadence = 3h** for Meetup and Luma/handoff-lane feeders (one-missed-cycle headroom inside the ≤6h target). **Recommendation: ratify.**
- [ ] **B25. [accept] Ticketmaster re-poll reserve sizing** — 500 calls/day supports ~125 concurrently watched TM events at 6h; overflow prioritizes soonest-starting events, alerts, and falls back to crawl delta. **Recommendation: ratify, confirm after G1** measures the request mix.
- [ ] **B26. [gate] The ~5 pts/Meetup-status-query figure is an ASSUMPTION the G2 spike must measure.** Under the per-app worst case at 100k users the watched-event poll load could exceed the quota ceiling; carried mitigations are per-token sharding, adaptive cadence for far-future events, and coverage-loss alerting (never silent staleness). **Recommendation: acknowledge** — folded into the G2 spike scope.

### B-V. Notification, engine & identity (ADR-009, ADR-010, ADR-011 — 8 riders)

- [ ] **B27. FORK — email-only launch channel consequence.** A hard-bouncing real address can reach TTL expiry without effective notice (task and TTL proceed while the user hears nothing). Mitigated by onboarding address verification + bounce alerting; SMS is the post-launch second channel. **Recommendation: accept** — SMS carrier registration (A2P 10DLC) adds weeks for zero launch need; onboarding verification closes most of the gap.
- [ ] **B28. [accept] NFR-2(b) measured at the SES delivery event** — the honest edge of our control; a greylisting recipient MTA can push the p99 tail past 120s and no architecture on our side removes that. Alert thresholds 45s/90s. **Recommendation: ratify the boundary + thresholds.**
- [ ] **B29. [accept] Fuzzy completion never auto-completes** — it costs one confirmation tap in exchange for zero wrong-task lifecycle advances (a wrong completion writes a calendar entry for an event the user never registered for). **Recommendation: ratify as product behavior.**
- [ ] **B30. FORK — Temporal outage posture.** Accept a bounded handoff-creation gap during an engine outage (no new handoff task for the window; intake buffers, in-flight state resumes losslessly) rather than build a second non-Temporal handoff path — a bounded NFR-2/NFR-15 exception. **Recommendation: accept** — a second independent handoff path is large effort against a rare, bounded, self-healing failure.
- [ ] **B31. [action/gate] O-6 Temporal Cloud contract checks.** SLA ≥ 99.9% (NFR-3), namespace throughput ≥ ~1,600 transitions/s peak, and the ~$25/M-actions cost basis are external list figures NOT dossier-backed; verify at contract before treating parent-workflow action volume as immaterial, and fall back to DBOS only if they fail. **Recommendation: verify during vendor procurement.**
- [ ] **B32. [gate] G3 — relay-address acceptance (still open, pre-design verification).** Do Luma/Eventbrite/Meetup signup validators accept `alice@u.<domain>`? If rejected, the OTP/magic-link path has no in-scope fallback (Gmail is forbidden) — resolve via a reputable dedicated domain / per-user real mailbox / first-login handoff. Gates FR-2.13 account linking. **Recommendation: run the G3 test early** (thread 3 builds the plan; it needs a relay domain from you).
- [ ] **B33. [accept] Real-email registrations are flagged poll-only.** A handoff user who registers with their real email (despite the relay prefill) silences the confirmation-match and change-email channels for that event; detection degrades to the public page (an invite-gated 403 page reduces it to nothing). Surfaced on the reconcile SLO dashboard. **Recommendation: ratify the coverage posture** (honest degradation, alerted not silent).
- [ ] **B34. [accept] Relay-prefill coupling.** When the user registers with the relay address, organizer emails route to a mailbox they do not monitor; they depend on our re-notifications for source-side changes. **Recommendation: ratify** — it is also what makes near-real-time change detection work.

### B-VI. Requirements fidelity fix (from the ADR indexer, 1 item)

- [ ] **B35. [accept] FR-2.7 egress-allowlist wording.** The requirement's letter says "target event origin + KMS endpoint only"; the decided design is tighter — "event origin + source first-party assets," because the guest session never needs KMS (the first-party broker reaches KMS from the vault segment, not the browser session). **Recommendation: amend the FR wording in v0.3 to match the tighter design** (a fidelity correction, not a weakening).

> Note: the rider count is 35 checkboxes across Part B; two ADRs bundle a paired sub-point (B15/B16 from ADR-006's kernel + transit, B31's SLA + cost), which is why the ADR index reports 37 rider *statements*. Every statement above is covered.

---

## Part C — Empirical gates that block build (independent of A/B)

These need real-world runs, not more design. Thread 3 of this session builds the G2/G3 harnesses so they execute fast once you provide credentials.

| Gate | What it decides | Blocks | Needs from you |
|---|---|---|---|
| **G2** (design-blocking) | Meetup `createEventRsvp`: auto-join open groups? quota per-token vs per-app? retry-idempotent? real `consumedPoints`? | Any autonomous-lane SLA (B14), Meetup pacing (B13), detection poll load (B26) | A Meetup Pro OAuth consumer (client id/secret) + a test account |
| **G3** (pre-design verification) | Do Luma/Eventbrite/Meetup signup validators accept `alice@u.<domain>` relay addresses? | The whole OTP/magic-link login leg + FR-2.13 account linking (B32) | A relay domain (`u.<domain>`) with inbound email routing |
| **G1** (estimation) | Realized autonomous / browser / handoff request mix | Re-sizes the TM re-poll reserve (B25) + browser pool + handoff staffing | Nothing new — runs against a realistic NL-request corpus |
| **P1–P7** (product validation) | NYC supply census, WTP smoke test, incumbent war-game, credential-grant willingness, private-graph share, tenure/CAC, TAM | Pricing (A2), naming (A4), metro-2 bar (A3), D9 scope | Field work (interviews, a landing test); P3 war-game is a desk exercise |

**Suggested critical path to build:** ratify Part A + Part B (this doc) → run G2 + G3 (thread-3 harnesses, need your credentials/domain) → fold D1–D8 + FR-2.7 into requirements v0.3 → build. P-gates run in parallel and gate GTM, not the build.
