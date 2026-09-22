# Events Concierge

Source of truth for the project. A fresh session should be able to resume from this file alone.

## Current milestone: private discovery candidate

The approved first release (September 10, 2026) is discovery/search, shared filters,
Events/Map/Calendar views, event details and provider registration links. Chat, automated RSVP,
managed handoffs, notifications, Calendar synchronization, purchases and programmatic API keys are deferred. The historical
build milestones and broader mission below are not acceptance of this release.

Private discovery acceptance is verified on the shared development stack. Production remains gated
on real identity/CSRF, transport, monitoring, permissions and provider-cleanup evidence.
Google sign-in is implemented for explicitly provisioned identities in the next release candidate.
OAuth configuration, private HTTPS activation and deployed identity acceptance remain pending.
Opt-in datastore TLS and self-hosted Temporal mTLS are implemented as
[transport preparation](deploy/development.md#encrypted-dependency-preparation); the active shared
development values remain unchanged and encrypted connections have not been accepted on GKE.
Google self-service account deletion stays unavailable until independent reauthentication is supported;
using that limitation for a private pilot requires an explicit scope decision. The existing OIDC
contract is recorded in [production operations](docs/production-operations.md#built-in-oidc-bff-activation).
[Release acceptance](docs/production-operations.md#first-release-acceptance) owns those gates;
[private deployment and recovery](deploy/development.md) owns current deployment and recovery operations. `EC_RELEASE_PROFILE=discovery` selects product scope; it does not
provision identity or turn mock integrations into production services.

## Mission

A personal AI **events concierge**: the user makes a natural-language request ("find me something Friday
evening after work and sign me up"), and the system **discovers** candidate events, **ranks** them against
the user's taste and constraints, **registers** on the event site autonomously, and **adds the confirmed
event to the calendar** (with de-duplication) — closing the loop end-to-end with the right amount of human
oversight.

## Prior art being rebooted

This is a **reboot** of a 2025 prototype the owner built, taking the *idea* forward but **changing the
approach for the 2026 agent stack**.

- **Repo:** https://github.com/iliazlobin/events-planner-agents
- **Video:** https://www.youtube.com/watch?v=ORLfWH-2Zfc ("Events Concierge AI Agent" demo)
- **2025 architecture:** supervisor-orchestrated multi-agent system.
  - Orchestration: **LangGraph** StateGraph supervisor ("event concierge") with `MessagesState` + `MemorySaver` checkpointing; `events_status` lifecycle (found → registered → scheduled_to_calendar).
  - Browser automation: **AutoGen MultimodalWebSurfer** customized, driving a real Chrome session over **CDP** (Playwright) — form-fill, checkboxes, dropdowns on Luma / Meetup.
  - Discovery: **OpenSearch** index with a custom `function_score` ranking (popularity, uniqueness, venue quality, food, proximity).
  - Calendar: **Google Calendar API** (OAuth), with dedup against existing events.
  - LLM: **OpenAI**, default `gpt-4o-mini`. Python 3.11.
  - Explicitly a **prototype / reference implementation**, meant for test accounts.

## Approach change for the 2026 reboot (THESIS — to validate in the research wave, not yet decided)

The "state of things now" delta versus the 2025 build. These are hypotheses to test in research/design,
**not** confirmed decisions.

- **Orchestration:** Claude Agent SDK / a native tool-use agent loop instead of the LangGraph supervisor graph.
- **Browser automation:** Claude computer-use / **Claude-in-Chrome** instead of AutoGen MultimodalWebSurfer + Playwright/CDP.
- **Reasoning model:** Claude (Opus 4.8 / Sonnet 5) instead of `gpt-4o-mini`.
- **Tools:** MCP servers (Google Calendar, browser, event-source connectors) instead of bespoke Python tool wrappers.
- **Discovery:** revisit self-hosted OpenSearch vs. live API/connector retrieval (Luma/Meetup/Eventbrite/partiful/ticketing APIs) + optional vector store; decide index-vs-live per source freshness.
- **Autonomy & safety:** re-examine where human-in-the-loop gates sit given stronger models and real money / real registrations at stake.

## Confirmed scope (HARD, dated)

Settled at kickoff **2026-07-02** — HARD, do not re-ask.

1. **Consumer surface = shareable product (multi-user).** Not a personal single-user tool. Implies:
   authentication, per-user profiles/preferences/credentials, tenant isolation, a hosted service, and
   product-grade NFRs (SLAs, security, secret management, possibly billing). This is the biggest design driver.
2. **Discovery model = hybrid (connectors + browser).** Curated API/connector retrieval where sources expose
   APIs/feeds (e.g. Eventbrite, Meetup, ticketing) **plus** Claude-in-Chrome browser discovery for
   API-less / JS-heavy / invite-only sites (Luma, Partiful). Carries forward the 2025 "real browser over
   fragile scrapers" insight as the fallback tier.
3. **Autonomy = fully autonomous, including paid/irreversible actions.** The agent registers / RSVPs /
   purchases end-to-end without a per-action confirmation gate. Because this is a *product* acting on
   *other users'* accounts and money, full autonomy raises the bar on: explicit per-user authorization &
   spending limits, idempotency, auditability, rollback/cancellation handling, and abuse/liability
   controls. Guardrails are policy-configured, not interactive prompts.

**Combined implication:** a multi-tenant product whose agents take autonomous, money-moving actions on
users' behalf — the design must treat authorization, safety, idempotency, auditability, and per-user
policy/limits as first-class, not afterthoughts.

Refined at **2026-07-02** after research wave 1 surfaced the risk profile — HARD, do not re-ask:

4. **Launch scope = FREE RSVP only; paid ticket purchase DEFERRED.** Autonomous free RSVP/registration
   (Luma, Meetup, Partiful, Eventbrite free events). Paid checkout is deferred behind the same policy flags.
   Rationale from research: autonomous paid checkout carries BOTS-Act legal exposure (Ticketmaster/Live
   Nation family), SCA/3-D Secure step-up that no unattended flow can answer, no idempotency key on browser
   checkout (double-charge risk), and default operator chargeback liability. **Design keeps the payment-port
   seam** (ACP `delegate_payment` / AP2 as a forward-looking abstraction) but ships it stubbed/disabled.
5. **Source posture = API-FIRST, browser BEST-EFFORT.** API-clean sources (Ticketmaster/SeatGeek discovery,
   Meetup, Bandsintown/Songkick) are first-class and SLA-guaranteed; browser-only sources (Partiful,
   Eventbrite discovery, Luma registration) are included but best-effort with human-handoff on failure — not
   on the reliability-SLA critical path. The per-source API-vs-browser fork is the central launch decision.
6. **Post-booking lifecycle = IN SCOPE for v1 (reconcile + un-RSVP).** The lifecycle extends past
   `scheduled_to_calendar`: detect organizer cancel/reschedule (schema.org `eventStatus` via webhook/poll per
   source) and reconcile the calendar entry; support user-initiated un-RSVP. Free-RSVP means no refund /
   money-moving complexity in this branch. Adds a change-detection subsystem + a cancel/reconcile saga branch.
7. **v1 framing = "ship the honest split"** (decided 2026-07-02 after research convergence exposed the Meetup
   membership-gate reality). The product presents as: **always-autonomous** discovery + ranking + calendar
   (+ reconciliation) across ALL sources; **autonomous RSVP** where the source permits — Meetup
   *existing-group* events (on-SLA) + Luma (browser, best-effort, off-SLA); **pre-filled one-tap human-handoff**
   everywhere else (Eventbrite, Partiful, Meetup approval/dues/non-member groups), positioned as a first-class
   product feature ("the concierge does 95%, you tap once"). Implication: TWO SLA classes (autonomous lane vs
   handoff lane), a first-class human-handoff subsystem, and per-source-modality routing are all in the FR set.

## Design values

- Production bullet-proof bar for a **multi-tenant product** (per the confirmed scope) — not a throwaway prototype.
- Real browser automation over fragile site-specific scrapers, as the fallback tier under API connectors (2025 build's key insight).
- Clean separation of concerns: reasoning/coordination vs. UI interaction vs. lifecycle state.
- **Autonomy with guardrails, not prompts:** since the agent acts fully autonomously on users' accounts and
  money, safety lives in per-user authorization, spending/policy limits, idempotency, auditability, and
  rollback — configured policy, not interactive confirmation.
- Zero-slop artifacts; every FR maps 1:1 to an executable acceptance check.

## Surfaces

- **Local workspace (authoritative for iteration):** `~/Claude/events-concierge/`. Preserve unrelated work when preparing commits.
- **Published 2026 source:** https://github.com/iliazlobin/events-concierge — private repository, remote `origin`, permanent integration branch `main`. Follow the [development workflow](README.md#development-workflow) for task branches, PRs and releases. The public 2025 repository remains the separate `legacy-prototype` remote.
- **Living Notion documentation:** [Events Concierge](https://app.notion.com/p/391d865005a8814181c1c508a5d70f34), directly under Workspace.
  - [Development](https://app.notion.com/p/3d6d865005a88185928bc16ba4883b31)
  - [Deployment](https://app.notion.com/p/3d3d865005a881339dc1f760bf5277e9)
  - [Application Design](https://app.notion.com/p/391d865005a88164a182eabc18fe068f) — includes component responsibilities.
  - [Application Infrastructure](https://app.notion.com/p/3a5d865005a881f288dfdc9993b8fdd2)
  - Do NOT touch the unrelated inline DB "System Design Interview Questions" (`62b6f08be0a74b81b3e48a203fa9e48d`).

## Conventions

- **Worker terminology (agreed 2026-09-07):** name background processes by their job: request-start worker, notification worker, change-delivery worker, watch-projection worker, and ingestion-command worker. Use these names consistently in code, logs, tests, diagrams and living documentation. Preserve historical evidence and proper provider names.

- **Code references (agreed 2026-09-07):** publish reviewed source changes to the approved GitHub repository before citing them in living Notion documentation. Use commit-pinned `blob/<full-commit-sha>/<path>` links for files and `tree/<full-commit-sha>/<path>` for directories; use verified line anchors when they clarify a specific mechanism. Display concise repository-relative labels rather than workstation paths. Verify each referenced path exists at that remote revision. Keep experimental branches and test dates distinct from deployed behavior; preserve dated historical evidence. If source is not yet published, label the reference pending instead of inventing a URL or linking an older revision as current. Preserve unrelated work and keep commits scoped; committing and pushing do not imply merging, deployment or migration.

- Arc/scaffold + persistence: `/project-kickoff` skill.
- Requirements + design craft and Workflow orchestration recipes: `/project-design` skill.
- Design-doc pyramid skeleton + rubric gate (≥33/40, no dim <3, critical dims ≥4): see `project-design` `references/system-design-craft.md`;
  rubric file `~/.hermes/skills/research/system-design-kanban/references/design-doc-rubric.md`; calibrator `~/.hermes/verify/score-design-doc.py`.

## State (update at every phase boundary)

Entries below preserve dated implementation history. The current release scope above and linked
deployment runbook take precedence; historical completions do not establish current deployment acceptance.

- **2026-07-02 — Kickoff / scaffold DONE.**
  - Local workspace scaffolded (all dirs + this file).
  - Prior art surveyed (repo README + video title) and captured above.
  - Notion hub "Events Concierge" + 4 artifact placeholder pages created under the "Claude" hub.
  - Auto-memory `project_events_concierge.md` + MEMORY.md index line written.
- **2026-07-02 — Scope confirmed (intake DONE).** Owner answered the 3 forks: shareable product (multi-user) ·
  hybrid discovery (connectors + browser) · fully autonomous incl. paid actions. Recorded HARD above.
  Design values updated to reflect multi-tenant + autonomy-with-guardrails.
- **2026-07-02 — Research wave 1 COMPLETE (Recipe 1, 9 dimensions).** Brief `research/00-research-brief.md`
  (validated the 2026 Claude-stack thesis with 2 load-bearing corrections: Claude-in-Chrome extension is
  OUT for the paid/registration leg — mandatory approval gate — use programmatic Computer Use API + DOM
  adapters; Agent SDK sessions are NOT durable execution — lifecycle belongs in an external Temporal-class
  engine). Completeness critic returned 12 gaps.
  - **Rate-limit incident:** 7 of 9 dossier *files* failed to flush (write agents hit server rate limits);
    all research + verify DATA survived in the run journal. Recovered via same-session `resumeFromRunId`
    (`wf_3ecf47d9-dd1`); all 9 dossiers `01`–`09` now on disk.
  - **Scope refined from the gaps** (decisions 4 + 5 above): free-RSVP-only launch + API-first/browser-best-effort.
- **2026-07-02 — Research wave 2 (gap remediation) LAUNCHED.** In flight. 6 probes for the surviving
  design-endangering gaps under the refined scope.
  - Script: `workflows/research-wave-2-gaps.js` · Run ID: `wf_699f6043-cc9` · Task ID: `w1064d1vl`
  - Transcript dir: `~/.claude/projects/-Users-iliazlobin/4a8ff50f-9cdc-4840-9f27-c0c1346806ff/subagents/workflows/wf_699f6043-cc9`
  - Probes → dossiers `10`–`15`: per-source-legal-tos · source-tier-reverify-2026 · google-oauth-casa-gate · durable-execution-engine · credential-injection-worker-isolation · capacity-quota-model.
  - Output → `research/00b-gap-remediation-addendum.md` + remaining-blockers critic.
  - Deferred (NOT in this wave): per-site browser success-rate = empirical build-time spike; payment rails / SCA = deferred with paid scope.
  - **Sharper re-run critic (recovery run `wf_3ecf47d9-dd1`, read all 9 dossiers) found 2 items wave 2 does NOT cover:**
    - **(A) Email/inbox ingestion — DESIGN-ENDANGERING, add to remediation.** Luma/Partiful (flagship free-RSVP browser targets) log in via email magic-link/OTP; without autonomous inbound-mail the agent cannot log in or confirm registration unattended. Collides with the "avoid Gmail scopes" plan. Candidate resolution: per-user concierge email alias/relay we control (no Gmail scope, no CASA escalation, smaller blast radius). → **fold as wave-2 probe `16-email-ingestion-architecture` via resume once wave 2 completes.**
    - **(B) Post-booking lifecycle (cancel/reschedule/refund/calendar reconciliation) — SCOPE DECISION.** Lifecycle stops at `scheduled_to_calendar`; organizer cancel/reschedule → stale calendar entry; user un-RSVP. Decide in-scope-at-launch vs explicit non-goal at requirements time (ask owner). Mechanisms (schema.org eventStatus + webhook/poll) are known; light research only if in-scope.
  - Other re-run-critic gaps already covered: per-source ToS/account-ban (probe 1), browser-worker injection reader/actor isolation (probe 5), bursty capacity ceilings (probe 6). Already deferred: payment card-injection-vs-ACP, per-site success measurement.
- **2026-07-02 — Research wave 2 (6 probes) COMPLETE.** Addendum `research/00b-gap-remediation-addendum.md`;
  dossiers `10`–`15`. Definitive **per-source launch matrix** (autonomous-register floor = Meetup API on-SLA +
  Luma browser best-effort; Eventbrite discovery-only/register-deferred; Partiful disabled by ToS). Durable
  spine = **Temporal Cloud** (DBOS fallback) + claim-check + detect-then-submit browser idempotency.
  **Calendar-only Google scopes = sensitive not restricted → dodge CASA Tier 2** (corrects wave 1) but need
  ~3–6wk sensitive-scope verification (pre-launch gate). Credentials = **BUILD** self-hosted KMS-envelope
  broker + Firecracker microVM per session + CaMeL dual-LLM boundary (1Password can't do unattended). Capacity:
  **Ticketmaster shared 5k/day app-key binds first (~4k users) → central read-through catalog cache is launch
  architecture**; browser concurrency (~40) is the real operational bottleneck; ~$0.15–0.25/confirmed RSVP.
  - **Wave-2 remaining-blockers critic → 2 gaps, both being closed via resume (`wf_699f6043-cc9`, probes 16+17):**
    - `16-email-ingestion-architecture` — magic-link/OTP login (Luma is passwordless) + confirmation vs the no-Gmail rule; per-user relay/plus-address channel.
    - `17-meetup-rsvp-prerequisite-chain` — does `createEventRsvp` work for non-members; is group-join (approval/screening/dues) API-automatable; Meetup Pro license revocation fallback. Load-bearing (sole on-SLA register path).
  - **Open SCOPE decision for owner:** post-booking lifecycle — RESOLVED (decision 6: reconcile + un-RSVP in v1).
- **2026-07-02 — Research CONVERGED (wave 2 + resume, dossiers `10`–`17` + addendum `00b`).** Probes `16`
  (email-ingestion) + `17` (meetup-rsvp-prereq) done. The remaining-blockers critic returned 3 gaps that are
  **all empirical / estimation, NOT more web research** → research phase is DONE; these become design-phase
  verification gates carried with pre-committed contingencies:
  - **LOAD-BEARING TRUTH — Meetup register() downgraded.** Meetup RSVP is membership-gated; there is **no
    `joinGroup` mutation**; approval/screening/dues groups → human-handoff. The on-SLA autonomous-register
    surface **shrinks to "Meetup groups the user is ALREADY in" + Luma (browser, best-effort, off-SLA)**.
    Eventbrite = discovery-only (register human-handoff); Partiful disabled. Discovery+rank+calendar remain
    fully autonomous across all sources. Meetup license is revocable/commercial-restricted = single point of failure.
  - **Empirical gate G1 (estimation, requirements input):** quantify the autonomous-on-SLA vs browser-best-effort
    vs human-handoff request mix by running a realistic NL-request corpus against the resolved source matrix →
    sets the SLA + handoff-lane sizing.
  - **Empirical gate G2 (design-BLOCKING build-time spike):** Meetup `createEventRsvp` — (a) does it auto-join
    an OPEN group for a non-member, (b) is the 500pt/60s quota per-token vs per-app vs per-IP, (c) is it
    retry-idempotent. Contingency to carry NOW: if per-app quota → global fair-share queue + degrade-to-handoff
    (no fixed SLA number); add a read-RSVP-state-before-mutate guard on the API adapter (API path currently has
    no double-RSVP guard — only the browser path does).
  - **Empirical gate G3 (pre-design verification):** do Luma/Eventbrite/Meetup signup validators ACCEPT the
    per-user relay-inbox addresses (`alice@u.concierge.app`)? If rejected, the OTP/magic-link login path has no
    in-scope fallback (Gmail is forbidden) → resolve before design freeze (reputable dedicated domain /
    per-user real mailbox / first-login human-handoff).
  - Also deferred (unchanged): per-site browser success-rate (empirical spike); payment rails/SCA (with paid scope); personalization-model lifecycle (design-phase tuning).
- **2026-07-02 — Research wave 1 LAUNCHED (Recipe 1, 9 dimensions).**
  - Script: `workflows/research-wave-1.js` · Run ID: `wf_3ecf47d9-dd1` · Task ID: `w9nbeotl5`
  - Transcript dir: `~/.claude/projects/-Users-iliazlobin/4a8ff50f-9cdc-4840-9f27-c0c1346806ff/subagents/workflows/wf_3ecf47d9-dd1`
  - Dimensions: agent-orchestration-2026 · autonomous-browser-registration · event-source-landscape · autonomous-action-safety · multitenancy-auth-secrets · calendar-integration · ranking-personalization · clean-architecture-agentic · cost-efficiency-scale.
  - Output → `research/NN-<dim>.md` + `research/00-research-brief.md` + completeness-critic gaps.
  - **Resume note:** `resumeFromRunId` works same-session only; cross-session relaunch fresh from the script. If it dies, salvage non-null `result` entries from the transcript `journal.jsonl` into `research/_salvage/`.

## Next steps

1. **Owner review of the DESIGN package — the current phase gate.** `design/system-design.md` (rubric PASS:
   39/40 + 38/40, lint + consistency clean) + `design/panels/01–04` (judge verdicts) + `decisions/` (11 ADRs;
   riders indexed in `decisions/README.md`). **37 ratification riders**; headline ones: 25-metro launch grid
   + far-window freshness degrade ladder (ADR-001/002); FR-8.2 parent-owns-discovery interpretation
   (ADR-003); 30-min kill-switch grace (ADR-004); 5-min Meetup DEGRADE threshold (ADR-005); AC-13
   vendor-attestation launch posture + fill-time CDP transit + computer-use injection-classifier opt-out
   (ADR-006); conflict-at-completion still writes the calendar (ADR-007); TM re-poll reserve sizing
   (ADR-008); email-only launch channel + SES delivery-event measurement boundary (ADR-009);
   Temporal-outage handoff-creation gap + O-6 contract checks (ADR-010); G3 relay-acceptance (ADR-011).
2. **Owner review of `design/product-opinion.md` v1.1** (still open, parallel) → **requirements v0.3
   fold-in**: D1–D8 evidence-stable, D9/D10 await ruling; plus the FR-2.7 egress-allowlist amendment
   (origin + source first-party assets; KMS via broker only) flagged by the ADR indexer; plus the
   freshness/anti-bot line items from `d20`/`d21`.
3. **Empirical gates before build:** G2 Meetup `createEventRsvp` spike (DESIGN-BLOCKING for any
   autonomous-lane SLA; gates ADR-005/008), G3 RelayInbox acceptance (gates ADR-011), G1 request-mix
   (re-sizes ADR-002/008 reserves); product gates P1–P7 from the opinion §11.
4. **Publish** (Recipe 6) into the Notion placeholders (System Design / Requirements / Decision Log /
   Research): system-design.md + panels + ADRs + requirements v0.2, plus the still-unpublished landscape
   dossiers `18`–`25`, brief `00c`, and the product opinion.
5. **Build phase** after ratification — ports-and-adapters implementation per the design; open with the
   G2/G3 spikes since both gate load-bearing lanes.

To resume: invoke `/project-design events-concierge` — reads this State and continues from the in-flight
wave or the next phase.

### Phase log (append-only)

- **2026-07-10 — PRODUCT-LANDSCAPE research wave LAUNCHED (owner ask: "research similar projects — open
  source, blogs, articles, products — collect as much info as possible; form product opinion").** The
  existing 17 dossiers are all *technical*; competitive/product landscape was an uncovered gap. 8 dimensions:
  commercial-event-discovery-products · agentic-assistants-that-book · open-source-event-aggregation ·
  indie-builds-and-writeups · academic-event-recsys-web-agents · market-failures-postmortems ·
  adjacent-concierge-scheduling · demand-signals-user-jobs. Pipeline research→verify→write (dossiers
  `18`–`25`) → synthesis `research/00c-product-landscape-brief.md` + product completeness critic.
  Script: `workflows/product-landscape-wave.js` · Run ID `wf_81474205-1bf` · Task `w3kpz6ilv` ·
  Transcript dir `~/.claude/projects/-Users-iliazlobin-Claude/d2853662-3729-417a-af67-782c2379936d/subagents/workflows/wf_81474205-1bf`.
  Deliverable after the wave: `design/product-opinion.md` (positioning, wedge, pricing, GTM, requirement
  deltas vs signed-off v0.2) written in the main loop + adversarial review pass. NOTE: the 2026-07-02
  design-phase judge-panel workflow (`workflows/judge-panels.js`) died with its session — no script or
  results on disk; design phase must relaunch after the product opinion lands.
- **2026-07-10 — PRODUCT-LANDSCAPE wave COMPLETE (with 2 salvage interventions) + PRODUCT OPINION v1.1
  WRITTEN. AWAITING OWNER REVIEW (phase gate).**
  - **Wave mechanics:** a verify agent hung indefinitely on a WebFetch to
    `framablog.org/2023/12/05/mobilisation-v4-the-maturity-stage/` (no harness timeout). Stop + resume
    (`resumeFromRunId`, same run `wf_81474205-1bf`, new task `wi1z4rx3l`) replayed the researcher prefix from
    cache but re-ran verify/write live — and a SECOND verifier hung on the SAME URL. Final recovery: stopped
    the workflow, salvaged the open-source dimension's research JSON + 6 verdicts from `journal.jsonl`, wrote
    dossier `20` in the main loop (claim 2 tagged unverified), ran synthesis + critic as direct Agent calls.
    **Lesson for future waves: one poisoned URL can wedge a pipeline twice; consider prompting verifiers to
    abandon a fetch that stalls, and salvage from the journal early.**
  - **Artifacts:** dossiers `research/18`–`25` (commercial products, agentic assistants, open source, indie
    builds, academic recsys/web-agents, market postmortems, adjacent concierges, demand signals — every
    load-bearing claim adversarially fact-checked), synthesis `research/00c-product-landscape-brief.md`,
    and `design/product-opinion.md` **v1.1** (positions + P-gates + 5 owner questions; v1.1 folds all 10
    findings of an adversarial fidelity review).
  - **Headline findings:** the closed loop (discover→rank→register→calendar→reconcile) is genuinely
    unclaimed as of 2026-07 across incumbents/horizontal agents/OSS/indies — but the window is closing
    (Posh $37M agentic framing; ChatGPT ticketing apps own NL discovery of PAID events; free/community
    events are the fee-less white space). Every horizontal agent gates exactly the RSVP/login step we
    automate. Trust cliff 74/32/9; pricing band $15–25/mo (labor, never inventory); wedge user = 25–40
    urban socially-displaced; NYC single-metro launch; north star = attended events/user/month, not DAU.
    Biggest unneutralized risks: supply scrape war (Bending Spoons owns Meetup + Eventbrite), thin
    autonomous surface share (→ P1 census), no-show amplification (→ delta D1 attendance loop).
  - **Product completeness critic → 7 gaps, all EMPIRICAL validation (not more web research)** → recorded
    as gates P1–P7 in the opinion §11.

- **2026-07-02 — Requirements phase LAUNCHED (Recipe 2).** Workflow `workflows/requirements-v1.js` ·
  Run ID `wf_130c29cf-57d` · Task `wp5ta3p1f`. Flow: staff engineer drafts `design/requirements.md` v0.1 from
  the research corpus (matching the jobs-tracking-system exemplar's 8-section format) → 3-lens adversarial
  panel (fidelity/completeness/testability) → fold every finding into v0.2 → closure-verify (verdict
  sign-off-ready | needs-another-round). On completion: present v0.2 + closure to owner for **sign-off**
  (the phase gate before design). The 3 empirical gates (G1/G2/G3) live in the requirements open-items ledger;
  G2 (Meetup spike) is DESIGN-BLOCKING.
- **2026-07-02 — Requirements v0.2 COMPLETE, closure verdict = SIGN-OFF-READY.** `design/requirements.md`:
  10 FR groups (FR-1..10), 75 acceptance criteria (AC-1..75), 19 NFRs across ISO 25010 (NFR-1..17 + 4a/4b),
  out-of-scope, settled-since-research, and the open-items ledger (G1/G2/G3 + O-4..O-10). 3-lens panel raised
  24 findings (1 blocker: the missing ranking→RSVP selection bridge, now FR-5.0); all folded into v0.2; closure
  agent confirmed all 24 RESOLVED, 0 blockers, only 2 minor new nits (both folded by hand: FR-6.6 RelayInbox is
  inbound-only; NFR-4 ITPM precision softened). **AWAITING OWNER SIGN-OFF (the phase gate before design).**
  The `[owner target]` numbers to ratify: FR-5.0 ≤3 attempt budget; FR-6.6 ≤7-day handoff TTL; NFR-1 ≤6h
  discovery / p95 ≤5s request; NFR-2 handoff p95 ≤60s task / ≤120s notify (autonomous-lane SLA DEFERRED to
  G1/G2); NFR-3 99.9% monthly; NFR-11 ≤72h erasure; NFR-13 ≤60s RPO / ≤24h RTO; NFR-17 ≤6h reconcile.
- **2026-07-02 — Requirements v0.2 SIGNED OFF by owner.** Owner targets ratified; autonomous-lane RSVP SLA
  stays deferred to G1/G2. Phase gate cleared → DESIGN.
- **2026-07-02 — DESIGN phase started. Judge panels (Recipe 3) LAUNCHED** on the 4 contested one-way forks →
  each verdict becomes one §6 deep dive + one ADR. Workflow `workflows/judge-panels.js`.
  Forks: (1) discovery & catalog-ingestion topology; (2) registration orchestration & honest-split lane
  routing; (3) credential/identity/browser-worker isolation topology; (4) human-handoff + lifecycle +
  organizer-change-detection subsystem. 3 divergent-bias proposers + 1 adversarial judge each.

- **2026-07-10 — DESIGN phase RESUMED by owner direction ("we've got the requirements — work on the system
  design"); judge panels RELAUNCHED.** Design targets the SIGNED-OFF requirements v0.2; the product-opinion
  review + v0.3 delta fold-in remain open in parallel (design keeps seams for D1/D2/D6 where structural).
  Same 4 forks as the dead 2026-07-02 run, now with fork-specific binding-constraint lists + divergent biases:
  (1) discovery & catalog-ingestion topology; (2) registration orchestration & honest-split lane routing;
  (3) credential/identity/browser-worker isolation topology; (4) human-handoff + lifecycle + change-detection.
  3 proposers + 1 adversarial judge + 1 writer per fork; verdicts land in `design/panels/0N-<key>.md`.
  Script: `workflows/judge-panels-v2.js` · Run ID `wf_14ccec66-e17` · Task `w0t3dutwr` ·
  Transcript dir `~/.claude/projects/-Users-iliazlobin-Claude/91b6702e-ab5d-4e9b-b5c2-f118cab6fe92/subagents/workflows/wf_14ccec66-e17`.
  **Skeleton ruling for the doc phase:** write to the 8-section pyramid (§7 Trade-offs + §8 References) per
  `project-design` craft + the jobs-tracking-system exemplar; the Hermes rubric file's 7-section/ASCII-only
  clauses are later Notion-content-pipeline additions — scorers will be instructed to apply the rubric's
  quality bars to the 8-section buildout shape (exemplar precedent: passed with §7 Trade-offs + em-dashes).
  After panels: system-design.md authored in the main loop → Recipe 4 rubric gate (2 scorers + linter +
  consistency adversary, ≥33/40) → Recipe 5 ADR log.
- **2026-07-10 — Judge panels COMPLETE (20/20 agents, 0 errors); `design/system-design.md` v1 WRITTEN;
  rubric gate round 1 = PASS on both scorers (A 34/40, B 36/40, all critical dims ≥4).** Verdicts in
  `design/panels/01–04`: (1) single-Postgres catalog + versioned-replay ingest + tenant overlay, 25-metro
  crawl at ~2,000/5,000 TM calls/day w/ decrement-first ledger; (2) two-tier Temporal parent(request)/
  child(user:event) + data-plane pre-mutate policy guard + off-engine Redis pacer (G2 contingency = config
  flip); (3) managed Browserbase fleet + first-party "sovereign trust plane" (sole-decrypt broker,
  pin-before-decrypt, sticky-identity egress, CaMeL split), gVisor self-host named migration at 70-concurrent
  tripwire; (4) DB-anchored lifecycle fn_transition + Temporal-as-executor + central change detection keyed
  by distinct watched events, email-only launch channel. Gate round 1 consistency adversary caught 1 MAJOR
  (doc silently took Panel 4's weaker <48h TM refresh over Panel 1's full-population 6h re-poller — a real
  Panel1/Panel4 conflict, resolved to Panel 1's stronger posture; must be recorded in the ADR log) + 2 minors
  (fn_transition retry-no-op control flow, §3 missing ×2-pages factor) — ALL folded, plus both scorers'
  improvement lists (Challenges→bullets, DD2 directive-protocol sequenceDiagram, §2/§3/§4 trims).
  Gate round 2 (verify) IN FLIGHT: Run ID `wf_097aa896-a79` · Task `wfxxdr433` · script
  `workflows/rubric-gate.js` (round 1 was `wf_70616f84-b50`). Next: ADR log (Recipe 5, ~11 ADRs seeded from
  panel verdicts incl. the Panel1/Panel4 detection reconciliation + owner-ratification riders) → PROJECT.md
  close-out → owner review.
- **2026-07-10 — DESIGN phase COMPLETE: rubric gate PASS + ADR log DONE. AWAITING OWNER REVIEW (phase gate
  before build).** Gate round 2 (`wf_097aa896-a79`): lint 0 findings, scorer A **39/40 PASS**, scorer B
  **38/40 PASS** (all critical dims ≥4 both cards); consistency down to 1 minor — the Panel1/Panel4 TM
  detection conflict needed EXPLICIT reconciliation → resolved: Panel 1's full-population 6h by-id re-poll
  is canonical (only variant where NFR-17 survives the budget degrade ladder); dated reconciliation note
  appended to `design/panels/04-handoff-lifecycle.md`; recorded in ADR-008. Round-2 convergent polish folded
  (approach-personification voice fixes, governance-provenance strips, §5 crawl-delta→CDS edge, FR4/FR5
  rewording, §3 trim). ADR workflow (`wf_d134a291-e6f`, script `workflows/adr-log.js`, 12/12 agents):
  `decisions/adr-001…011` + `decisions/README.md` — indexer cross-checked every shared number vs doc +
  requirements: **0 numeric fixes needed, 0 judgment-bearing contradictions, 37 owner-ratification riders**
  indexed; open gates mapped (G1→ADR-002/008 sizing, G2→ADR-005/008, G3→ADR-011). One requirements
  amendment flagged for v0.3: FR-2.7 egress-allowlist letter says "origin + KMS endpoint"; the decided
  design is tighter ("origin + source first-party assets", KMS reached only by the broker from the vault
  segment) — amend the FR wording rather than weaken the design.

- **2026-07-14 — OPEN-ITEMS SWEEP (owner ran /effort ultracode + "work on whats still open", chose "all of it,
  in order"). Three parallel threads:**
  - **Thread 1 — Publish + decision brief.** `design/owner-decisions.md` WRITTEN: 5 strategic rulings (A1-A5) +
    37 technical riders (B1-B35, forks flagged) as checkboxes w/ recommendations + Part C gate map. Notion
    publish workflow `wf_c12d78c7-9ee` (script `workflows/notion-publish.js`) — 4 agents, one per hub
    placeholder (System Design+4 panels, Requirements, Decision Log README+brief+11 ADRs, Research index+28
    dossiers); sub-agents CONFIRMED to have claude.ai Notion access. Notion conventions: `yellow_bg` (not
    yellow_background), mermaid fences pass literally. STATUS: COMPLETE + VERIFIED (0 errors) — 4 placeholders + 44 child pages (System Design+4 panels, Requirements v0.3, Decision Log README+Owner Brief+11 ADRs, Research+28 dossiers); Requirements re-published with final reviewed v0.3; hub blurb refreshed. (The VERIFY/re-publish notes below were the plan, now done.)
    VERIFY all pages/children on completion; RE-PUBLISH the Requirements page with final v0.3 (a race meant
    it may have caught mid-edit v0.2/v0.3 — the page should show the DRAFT-marked v0.3).
  - **Thread 2 — Requirements v0.3 DRAFT — DONE, sign-off-ready pending owner A5.** `design/requirements.md`
    now v0.3 (v0.2 was untracked/overwritten; its content is in the changelog + session history). FR-2.7
    fidelity fix + FR-11..18 (D1-D8) + NFR-18 + reconciling touches to NFR-6/FR-6.3/AC-43/FR-6.6/FR-7.1/
    FR-10.5/NFR-11/NFR-16; ACs AC-76..96. D9/D10 HELD (§8). Two adversarial passes folded: review workflow
    `wf_b6cde005-517` (24 findings incl. the NFR-6 blocker my own FR-2.7 fix missed + a D10 leak) + a
    closure re-verify agent (5 additive coverage gaps: attendance inbound contract FR-11.8, erasure AC
    coverage, etc.). All 29 findings folded; self-verified clean (96 ACs, no dupes, no D10 leak, all
    owner-targets labelled). Scripts: `workflows/reqs-v03-review.js`.
  - **Thread 3 — G2/G3 spike harnesses — DONE.** `spikes/g2-meetup-rsvp/` (Meetup createEventRsvp probe:
    introspects live schema, dry-run default, answers auto-join/quota-scope/idempotency/point-cost →
    ADR-005/008) + `spikes/g3-relay-acceptance/` (relay receiver = working FR-5.7/5.8 EmailIngestionPort
    prototype + per-source acceptance test plan). Both syntax-clean, blocked only on owner creds (Meetup Pro
    OAuth; relay domain w/ inbound email). `spikes/README.md` + per-spike RUNBOOK.md.
  - **SWEEP COMPLETE (2026-07-14).** All three threads done + verified. Remaining is owner-gated only:
    (1) rule on `design/owner-decisions.md` — 5 strategic questions (A1-A5) + 37 riders (B1-B35); (2) provide
    creds to run the G2 (Meetup Pro OAuth) + G3 (relay domain) spikes; (3) sign off requirements v0.3 (A5).
    On v0.3 sign-off, re-publish the Requirements Notion page + flip its header from DRAFT. Then build.

- **2026-07-15 — BUILD PHASE STARTED: foundation DONE + fully verified.** Owner approved build ("start
  building foundation, clean code, solid design"; accept-all-defaults; free-crawl-first discovery kept
  clean for future API swap; recommendations = personalized ranked feed as a scroll; docker-compose deps,
  mock AWS/Google/others). Stack: Python 3.12 (uv), Temporal, Postgres 16 + pgvector, Redis; cloud mocked.
  Clean hexagonal architecture at repo root: `src/events_concierge/{domain,ports,application,adapters,
  workflows,api}` + `infra`, `composition.py`, `slice_demo.py`; `migrations/` (3, Alembic); `tests/`;
  `pyproject.toml`/`Makefile`/`docker-compose.yml`. **62 src modules, ~3,600 LOC.** Verified green: mypy
  strict 0 issues, ruff clean, **32 tests** (27 unit + 5 integration). The end-to-end slice + integration
  tests exercise BOTH honest-split lanes (Meetup member → autonomous RSVP + deterministic-id calendar
  write; free-crawl → handoff task), the two-tier Temporal spine (parent→child→activity, on the in-process
  test server), and real RLS tenant isolation (AC-1). Adapters built via a fan-out workflow
  (`wf_0251f002-c49`) against frozen ports; coupled spine authored in the main loop.
  - **Ports: postgres 5433 / redis 6380 / temporal 7234 / temporal-ui 8234** (5432 etc. were taken by
    another local stack). **RLS gotcha (load-bearing):** the bootstrap `ec` role is a superuser and
    BYPASSES RLS even under FORCE; the app MUST connect as the non-superuser **`ec_app`** role (migration
    0002) for isolation to hold, and policies use `NULLIF(current_setting('app.tenant_id',true),'')::uuid`
    (migration 0003) so an empty context fails closed to zero rows, not a cast error. Migrations run as
    owner (`EC_MIGRATION_URL`); app runs as `ec_app` (`EC_DATABASE_URL`).
  - **Run:** `make install && make up && make migrate && make test` (unit) / `make test-integration` /
    `make slice`. NOT committed to git (owner didn't ask). Deliberate simplifications flagged in code:
    heuristic parser (real=Claude), single-activity register saga (real=granular+compensations), pacer
    built but not yet on the register path, mocks for KMS/SES/Calendar/broker.
  - **NEXT build steps:** real source adapters (Meetup/Luma via the G2/G3 spikes once creds land), the
    granular saga + compensations, the Rust injection broker + real KMS vault, real Google Calendar +
    RelayInbox, notifier/outbox relay, the ranking cross-encoder + LightGBM re-score.

- **2026-07-16 — BUILD MILESTONE 1 COMPLETE: granular Temporal registration saga + exactly-once
  recovery.** Replaced the single `register_candidate` activity with the ADR-003 sequence
  `resolve_membership → policy_gate → register_or_rsvp → await_confirmation → dedupe_calendar →
  write_to_calendar`. The child mints source, transition, calendar-recovery, and handoff identifiers once
  in workflow state; source mutations have one Temporal attempt and explicit recovery re-enters through a
  paced remote-state read, never a blind re-POST. The parent/child directive protocol now preserves the
  ≤3-candidate loop: a failed child parks for `close` or `demote_to_handoff`, and only the selected child
  becomes the terminal handoff. Confirmation uses an opaque signal reference plus a fresh paced remote read;
  pending-state ACK loss is modeled explicitly. Calendar failures preserve the factual `REGISTERED` state and
  atomically create one stable manual-recovery task plus `calendar_recovery_required` outbox row.
  - **Coverage / verification:** lost source ACK, lost pending ACK, signal-triggered remote confirmation
    verification, 24-hour timeout, lost calendar ACK, permanent calendar failure + compensation retry, and
    candidate fall-through are covered in the Temporal integration suite. `mypy` strict passes (**62 source files**); `make test-integration`
    passes (**11 integration tests**); the existing unit suite passes (**27 tests**). Temporal Compose health
    check was corrected to address the service hostname, so `make up` now waits green.
  - **NEXT:** build the durable outbox relay/notifier worker (ADR-009), then the offline-safe ranking and real
    source/calendar adapter scaffolds. The v0.3 D1–D8 deltas remain deferred pending owner sign-off.

- **2026-07-16 — BUILD MILESTONE 2 COMPLETE: transactional-outbox relay + notifier worker.** Migration
  `0004` gives each committed outbox row a lease, bounded retry schedule, error/failure audit fields, and a
  durable `notification_ledger` keyed by the stable workflow/transition notification key. `OutboxRelay` leases
  rows with `SKIP LOCKED`, maps only user-facing lifecycle topics to `NotificationPort`, retries at
  30s/2m/10m/30m, and records a terminal fifth failure without losing audit truth. A visible send followed by a
  process/ACK loss reuses that same key; the ledger plus port contract keeps it at-most-once user-visible while
  delivery remains at-least-once. `events_concierge.workers.notifier` is the independent poll worker and
  `make notifier` starts it locally.
  - **Coverage / verification:** focused unit tests cover lost notifier acknowledgement and terminal retry;
    the Postgres integration test proves a guarded `lifecycle.handoff` transition becomes one durable ledger
    delivery and remains deduplicated on a second poll. Ruff and strict mypy pass; the full suites pass
    (**29 unit, 12 integration**).
  - **NEXT:** add the offline-safe cross-encoder and per-user re-score behind `RankerPort`, retaining the
    deterministic embedding adapter as the mock/offline double.

- **2026-07-16 — BUILD MILESTONE 3 COMPLETE: swappable cross-encoder + per-user re-score.** The
  post-retrieval stack is now explicit behind `CrossEncoderPort`, `RankingProfilePort`, and
  `FeatureRescorerPort`, with `PersonalizedRanker` assembling cross-encoder relevance, request↔event cosine,
  declared affinity, and aggregated implicit-affinity features into a stable final score. A tenant with no
  profile receives neutral affinity values, preserving the FR-4.4 cold-start floor without implementing the
  deferred v0.3 onboarding/attendance deltas. `LightGBMFeatureRescorer` exposes a narrow loaded-model inference
  boundary; production can inject it alongside the concrete Cohere adapter.
  - **Concrete/offline split:** `CohereRerankCrossEncoder` implements the documented v2 `rerank-v3.5` request
    shape through an injected `httpx` client, validates complete indexed responses, and never opens a network
    connection during composition. Default/mock composition uses the deterministic joint-token cross-encoder,
    in-memory neutral profiles, deterministic feature re-score, and the existing deterministic embedding.
  - **Coverage / verification:** offline transport coverage asserts the Cohere request/response contract;
    ranking tests prove neutral cold-start behavior, per-user profile reordering over the same candidate set,
    and the stable LightGBM feature row. Focused ranking tests, Ruff, and strict mypy pass (**66 source files**).
  - **NEXT:** scaffold fixture-backed Meetup API and Luma browser `SourcePort` adapters, explicitly disabled
    pending the owner-run G2/G3 acceptance spikes.

- **2026-07-16 — BUILD MILESTONE 4 COMPLETE: fixture-backed Meetup and Luma registration adapters.**
  `RegistrationTarget` now carries the retained source event ID and original registration URL through the
  `SourcePort`, so a deduplicated CanonicalEvent keeps the exact source surface needed for safe execution.
  The registration saga freshly reads Meetup membership under a Pacer lease before permitting the API lane;
  it fails closed to non-autonomous lanes unless the user is already a `MEMBER`. This supersedes the feed-time
  membership hint without broadening discovery or onboarding scope.
  - **Meetup:** `MeetupSource` is an API-only, disabled-by-default adapter over narrow token/API ports. Its
    fixture-tested GraphQL transport performs separate membership and RSVP reads and issues one provisional
    `createEventRsvp(input: {eventId, response: YES})` mutation, explicitly omitting `member_id` and any
    invented vendor idempotency field. Workflow-minted keys remain the local recovery invariant.
  - **Luma:** `LumaSource` accepts only a typed browser observation port—never DOM, screenshots, credentials,
    or OTPs. It fresh-detects before each submit, permits submit only for a known-free `NOT_PRESENT` state,
    maps login/paywall/ambiguous/CAPTCHA safely to reauth/handoff, and models lost acknowledgement by
    re-detecting `CONFIRMED` with zero second submit.
  - **Coverage / verification:** sanitized JSON fixtures drive Meetup transport, membership/RVSP mapping, OAuth
    rejection, Luma free-price gating, unsafe zero-submit outcomes, and the lost-ACK regression. Focused adapter
    tests pass (**9 tests**); the full granular Temporal saga suite passes (**7 tests**) with the new paced
    membership trace; Ruff and strict mypy pass (**73 source files**).
  - **Activation gate:** these adapters are deliberately not wired into default composition. The owner must run
    **G2** with a Meetup Pro OAuth consumer/test accounts and **G3** with a relay domain before any live enablement,
    SLA, Browserbase/session capture, or schema/quota assumption is accepted.
  - **NEXT:** add the swappable Google Calendar adapter behind `CalendarPort`, retaining `MockCalendar` for
    default/offline composition.

- **2026-07-16 — BUILD MILESTONE 5 COMPLETE: swappable Google Calendar adapter scaffold.**
  `GoogleCalendarAdapter` is a real, transport-injectable Calendar v3 implementation behind
  `CalendarPort`: it queries only an explicit tenant binding's free/busy calendars, writes the deterministic
  event ID with IANA time zones and private canonical/source/state metadata, then converges a retry through
  `insert → HTTP 409 → patch`. Deletes tolerate a prior remote deletion, malformed free/busy responses fail
  closed, and authorization failures are surfaced as the typed, single re-consent condition. The
  `CalendarEntry` contract now carries defaulted private metadata, while `RegistrationService` normalizes
  offset-only source times to UTC and preserves valid IANA zones.
  - **Persistence and composition:** migration `0005` adds an RLS/FORCE-RLS `calendar_bindings` table with
    the fail-closed `NULLIF(current_setting(...), '')::uuid` policy, a tenant FK, and non-secret write/free-busy
    calendar IDs only. `GoogleCalendarAccessPort` isolates short-lived bearer-token resolution from the
    adapter; `GoogleCalendarBindingPort` persists the already-provisioned app-calendar target. The container
    remains `MockCalendar` by default and accepts an injected `CalendarPort` for a provisioned deployment.
  - **Coverage / verification:** offline `httpx.MockTransport` fixtures prove free/busy request and response
    handling, deterministic insert/conflict/patch, idempotent delete, re-consent, missing binding, scope
    allowlist, and IANA payloads. The Postgres integration test proves binding tenant isolation and empty-GUC
    fail-closed behavior. Full suites pass (**48 unit, 13 integration**); Ruff and strict mypy pass
    (**77 source files**).
  - **Activation boundary:** no external account, OAuth client, refresh-token storage, or calendar creation was
    assumed. A live tenant must first complete owner-provisioned OAuth and bind an app-created secondary
    calendar; the adapter never falls back to `primary`. This preserves the required calendar-only scope
    posture while leaving the unresolved provisioning/scope activation decision to the owner.
  - **NEXT:** D1–D8 remain deferred until requirements v0.3 is owner-signed; Google live activation likewise
    remains disabled until its owner-provisioned OAuth/binding path is approved.

- **2026-07-16 — BUILD MILESTONE 6 COMPLETE: opt-in, no-Pro public calendar discovery.**
  The generic public JSON-LD ACL now recognizes schema.org `ItemList.itemListElement[].item` events, the
  shape exposed by an owner-approved public Luma calendar. Explicit non-zero offers are now marked paid and
  excluded by the free-only launch constraint; elapsed listings are excluded at the source boundary. The new
  comma-separated `EC_CRAWL_SEED_URLS` setting is empty by default, so every live crawl remains an explicit
  owner-approved opt-in. Composition passes only the allowlisted `public_jsonld` source into the paced
  `PublicJsonLdSource`, so `EC_DISCOVERY_SOURCES` can disable a populated seed without any fetch.
  - **Live read-only smoke:** the approved `https://luma.com/genai-sf` calendar yielded **18 future,
    non-explicitly-paid listings** on 2026-07-16, including the OpenAI Build Week Community Meetup and July
    18–22 events. No sign-in, browser session, RSVP attempt, calendar write, or source mutation occurred.
  - **Safety / routing:** the listings retain `Source.PUBLIC_JSONLD`, so their only registration route is
    human handoff. This adds no Luma credential, Browserbase/CDP, OTP, or autonomous-browser behavior, and
    leaves Meetup disabled pending its Pro/API gate.
  - **Coverage / verification:** unit tests cover ItemList extraction, explicit paid filtering, elapsed-event
    suppression, per-host pacing, seed parsing, and source-disable behavior. The final non-Meetup bar passes
    (**57 unit, 14 integration**); Ruff and strict mypy pass (**77 source files**).
  - **NEXT:** when the owner supplies a controlled Google OAuth/calendar binding, run the Google smoke test;
    when a relay domain/inbound mailbox is available, run G3. Neither requires enabling Meetup.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 1 COMPLETE: truthful paid-event discovery.**
  The owner directed the catalog toward Bay Area coverage (working boundary: the standard nine counties) and
  explicitly approved showing paid listings in discovery. This supersedes the prior free-only *serving*
  posture, but does not authorize paid purchase, checkout, or autonomous registration: those remain disabled
  and route to handoff.
  - **Price truth / provenance:** migrations `0006` and `0007` replace the lossy canonical `is_free` boolean
    with `free | paid | unknown` and retain the current observation on every `event_source_links` row.
    Canonical `FREE` now requires every retained source link to verify free; mixed, tiered, donation, missing,
    or conflicting offers are `UNKNOWN` and fail closed. Legacy rows that once collapsed an absent price into
    `true` are migrated conservatively, and a rollback maps only verified-free rows back to `true`.
  - **Discovery / safety:** ordinary requests retain free, paid, and unknown future listings; an explicit
    free-only hard constraint returns only verified-free listings. The API exposes `price_status`. The
    registration saga rejects paid or unknown canonicals before a source read or mutation and additionally
    refuses a non-free source link even if a malformed aggregate claims `FREE` (FR-3.7, FR-4.6, FR-5.10).
  - **Coverage / verification:** tests cover mixed JSON-LD tiers, unknown-price handling, per-link
    free/paid conflict followed by refresh, free-only retrieval, and zero source effects for a paid target.
    Integration fixtures now use full UUID discriminators so repeated local runs cannot fuzzy-dedup into stale
    test records. `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**68 unit, 16 integration; 77 source files**).
  - **NEXT:** build the durable reviewed-source registry and off-request refresh worker before enabling a
    broader Bay Area cohort. The sole live public seed remains the explicitly owner-approved
    `https://luma.com/genai-sf`; no blanket Luma/platform crawl, Meetup work, credentials, sign-in, RSVP, or
    calendar write is enabled.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 2 COMPLETE: reviewed source registry and off-request refresh.**
  Migration `0008` adds tenant-neutral `catalog_sources` and `catalog_refresh_runs`. The registry carries a
  stable source key, publisher/display name, HTTPS seed and approved origins, region, parser mode,
  handoff-only capability, owner-review/expiry state, cadence, and per-source pacing floor. Its sole seeded
  record is the user-approved `luma-genai-sf` calendar; being enabled makes it eligible for an explicit refresh,
  not an automatic HTTP call.
  - **Durability / safety:** `CatalogRefreshService` claims a `(source_key, run_key)` lease before any fetch;
    completed keys make zero repeated source calls, while failed/expired leases can be reclaimed. It takes the
    shared Pacer lease, and the JSON-LD adapter manually follows only redirects whose final origin remains in
    the source allowlist. Disabled, unreviewed, expired, unknown, or unsupported records make zero external
    calls. Public registry sources are structurally handoff-only.
  - **Operational boundary:** `make catalog-refresh SOURCE_KEY=luma-genai-sf` is the explicit read-only manual
    entrypoint (optionally add `RUN_KEY=...` for a stable retry). `/v1/feed`, `/v1/requests`, and the Temporal
    parent activity now read the persisted catalog only; they never crawl a website as a side effect of user
    intake. A Temporal Schedule/cadence runner is deliberately the next step rather than being inferred here.
  - **Live read-only smoke:** one explicit `luma-genai-sf` refresh on 2026-07-16 Pacific time (logged
    2026-07-17 UTC) fetched **20** future public listings and upserted **20** canonicals. The observed set
    included one explicit paid listing, `Scrappy AI Founders Go Mountain Biking`, which remains discovery-only
    and non-autonomous; no sign-in, browser session, RSVP, payment, calendar write, or source mutation occurred.
    The duplicate-run replay command was not executed because the local permission reviewer timed out; unit and
    PostgreSQL coverage already prove the completed-run no-op path.
  - **Coverage / verification:** unit coverage proves completed-run dedup, failure/retry, approval gates,
    Pacer use, API no-fetch behavior, and redirect rejection before an unapproved origin is requested.
    PostgreSQL coverage proves the seeded registry plus lease/busy/success/failure-reclaim transitions.
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    passes (**75 unit, 17 integration; 82 source files**).
  - **NEXT:** add item-level source/run provenance and the first reviewed civic/university adapters through this
    registry, then attach a bounded Temporal Schedule. Do not enable generic platform-wide crawling, Meetup,
    sign-in, RSVP, payment, or calendar mutation.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 3 COMPLETE: item-level source/run provenance.**
  Migration `0009` adds tenant-neutral `catalog_event_observations`, keyed by reviewed source, generic source
  kind, and external source event ID. A successful registry refresh now persists each normalized event's
  canonical attachment, registration URL, explicit price state, deterministic normalized-content hash,
  first/last-seen timestamps, and exact `catalog_refresh_runs` key. This preserves the source that supplied a
  result even while canonical dedup retains one cross-source event identity.
  - **Failure semantics:** observations are written only after catalog upsert and before the refresh run is
    marked successful. A crash/failure leaves the run retryable; re-entry deterministically repairs the
    observation rather than claiming an unobserved fetch succeeded. The initial Luma run predates this table,
    so its observations will appear on the next explicit refresh rather than being fabricated retroactively.
    Raw page payload retention remains intentionally deferred; the stored hash covers normalized public fields
    only and avoids accidentally persisting unreviewed page content.
  - **Live read-only smoke:** a fresh `luma-genai-sf` run wrote **20** source/run observations attached to
    **20** canonicals (**19 free, 1 paid, 0 unknown**), verified through the `ec_app` role. It again performed
    no sign-in, RSVP, payment, calendar write, or source mutation.
  - **Coverage / verification:** service tests assert source/run provenance is recorded exactly once with a
    completed refresh; PostgreSQL coverage verifies its source/run FK, canonical attachment, and content hash.
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    passes (**76 unit, 17 integration; 83 source files**).
  - **NEXT:** run a new explicit Luma refresh to populate provenance, then add the first reviewed official
    JSON-LD Bay Area calendars through the registry. Keep all of them read-only and handoff-only; a bounded
    Temporal Schedule remains a separate follow-up.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 4 COMPLETE: first official publisher-calendar cohort.**
  Migration `0010` adds three reviewed, publisher-owned, Bay Area JSON-LD seeds to `catalog_sources`:
  `stanford-events` (`events.stanford.edu`), `ucsf-events` (`calendar.ucsf.edu`), and `sjsu-events`
  (`events.sjsu.edu`). Each is explicitly HTTPS-origin allowlisted, `bay_area_9_county` tagged, enabled only
  for an explicit worker/manual refresh, and structurally handoff-only. No generic Luma discovery page,
  Eventbrite/Partiful/Meetup platform crawl, credential, or browser automation was enabled.
  - **Coverage / verification:** PostgreSQL integration coverage asserts the exact reviewed cohort and its
    handoff-only/region/seed URL properties. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**76 unit, 18 integration; 83 source files**).
  - **NEXT:** run one bounded read-only refresh per reviewed publisher calendar and inspect counts/quality;
    then add source-specific API/RSS adapters (SF.gov, Berkeley LiveWhale, etc.) as separate ports rather than
    weakening the JSON-LD boundary. Temporal cadence scheduling remains separate.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 5 COMPLETE: duplicate calendar-URL protection.**
  Live quality inspection found that some publisher pages use one calendar homepage URL for multiple JSON-LD
  events. A URL-only source ID would overwrite unrelated `event_source_links` and source/run observations.
  The ACL now appends a deterministic title/start suffix **only when that source ID repeats within one parsed
  document**; normal event-detail URLs and truly duplicate JSON-LD copies remain stable. This repairs the next
  refresh without reminting canonical IDs and protects provenance/dedup before wider source expansion.
  - **Exact-copy handling:** a follow-up inspection showed the remaining count gap was repeated identical
    JSON-LD blocks, not missing distinct cards. Exact copies now collapse to one richer, price-conservative
    source observation; same-title/time cards at different locations remain separate. Refresh metrics report
    distinct canonical IDs rather than repeated upsert rows.
  - **Coverage / verification:** regression fixtures prove both distinct shared-URL cards and exact duplicate
    blocks behave safely. `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**78 unit, 18 integration; 83 source files**).
  - **NEXT:** re-refresh the affected reviewed calendars under new run keys, inspect corrected source-link and
    provenance counts, then continue with official API/RSS adapters only after their own fixture-backed ports.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 6 COMPLETE: bounded LiveWhale publisher feeds.** Migration
  `0011` extends the reviewed registry with a positive `page_limit` and the separate
  `livewhale_json` ingestion mode; `0012` requests only the documented `location`, `summary`, and
  `description` response fields needed for a useful catalog. Together they seed only three publisher-owned,
  anonymous JSON feeds: UC Berkeley, Santa Clara University, and San Mateo County Community College District.
  Each is HTTPS-origin allowlisted, `bay_area_9_county` tagged, enabled only for an explicit manual/worker
  refresh, handoff-only, paced at a 1.5-second per-host floor, and initially capped at three 100-record pages
  per run. That cap is a bounded quality/safety pass—not a claim to crawl every LiveWhale result—and does not
  enable a platform-wide crawl.
  - **Adapter / data truth:** `LiveWhaleCatalogFetcher` follows same-origin canonical redirects and pagination
    only after validating every target against the registry. It parses offset-aware `date_iso`/`date2_iso`,
    recurring occurrences, publisher URLs, optional published venue/city/coordinates, and HTML descriptions.
    Event IDs include the reviewed source key plus the publisher ID and occurrence timestamp, preventing both
    recurring-event and cross-publisher collisions in the generic `(source, source_event_id)` link key. A
    richer repeat observation can fill only blank canonical venue/geo/end/description metadata, preserving its
    canonical identity and any existing nonblank value. Public cost text is conservatively
    `free | paid | unknown`; mixed/conditional tiers remain unknown. Canceled and source-declared `Online only`
    listings are excluded, while explicitly hybrid local listings remain eligible.
  - **Safety / routing:** this adapter performs only read-only HTTP GETs to approved publisher origins. It
    neither signs in nor fetches a registration target; all resulting `PUBLIC_JSONLD` candidates remain on the
    existing human-handoff route, so no RSVP, payment, calendar write, credential, or browser behavior is added.
  - **Coverage / verification:** offline `httpx.MockTransport` tests cover page bounds, pacing, same-record
    price truth, canceled/numeric-online/hybrid handling, source-keyed recurring IDs, and rejection before an
    unapproved redirect or pagination target is requested. Registry integration coverage asserts each exact
    seed/mode/page cap; a catalog integration test proves a richer repeat observation fills only absent metadata.
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    passes (**81 unit, 19 integration; 85 source files**).
  - **NEXT:** validate the initial publisher refreshes and retain the three-page cap until quality inspection
    supports expanding it; SF.gov, DataSF, and RSS feeds remain separate fixture-backed adapter increments.

- **2026-07-16 — BAY AREA CATALOG LIVE VALIDATION 1: reviewed LiveWhale cohort.** Explicit read-only manual
  refreshes exercised all three reviewed feeds through their approved origins and documented response fields:
  Berkeley produced **268 observations / 168 canonical attachments**, SCU **272 / 250**, and SMCCD **186 / 172**.
  Each request was bounded to three pages, so every reported result is an intentionally bounded subset of the
  publisher's public catalog. This is reviewed public coverage, not a literal claim of “all Bay Area events.”
  - **Berkeley quality:** the final corrected run has 9 verified-free, 3 paid, and 256 unknown observations;
    all 168 attached canonicals have venue text, 167 have a description, and 58 have coordinates. The publisher
    does not supply a city field, so no city was inferred. An earlier smoke pass was cleaned of 32 local,
    source-declared remote-only artifacts after the numeric `is_online=1` representation was discovered; the
    final source provenance contains only the 268 corrected observations.
  - **SCU / SMCCD quality:** SCU currently reports 7 free, 60 paid, and 205 unknown observations, with 196
    venue, 230 description, and 121 coordinate observations. SMCCD currently reports 186 unknown-price
    observations, with 149 venue, 151 description, and 126 coordinate observations. Missing/ambiguous prices
    remain discovery-only and never authorize autonomous registration. Neither publisher provides a reliable
    city field in this endpoint, so the reviewed publisher/region boundary is retained rather than fabricating
    location data.
  - **Safety:** all live calls were public `GET`s only. No account, credential, sign-in, RSVP, purchase,
    registration-target fetch, calendar write, or source mutation occurred. Catalog candidates remain
    handoff-only.
  - **NEXT:** maintain explicit/manual refresh while assessing source freshness and the three-page quality cap;
    add the SF.gov, DataSF, and RSS adapters only with their own offline fixtures and reviewed registry records.

- **2026-07-16 — BAY AREA CATALOG INCREMENT 7 COMPLETE: SF.gov civic API.** Migration `0013` adds the
  separately typed `sf_gov_json` mode and one reviewed City and County of San Francisco source:
  `sf-gov-related-events`. Its fetch allowlist is only `https://api.sf.gov`; the adapter itself is constrained
  to the owner-reviewed, anonymous upcoming/date-grouped related-events endpoint rather than accepting an
  arbitrary API path. It is handoff-only, paced at 1.5 seconds per host, and refreshes every six hours when a
  scheduler is later enabled.
  - **Completeness / failure posture:** SF.gov supplies a reported total but no server-provided next URL or
    page-size metadata. The adapter constructs the approved page sequence itself, flattens each date bucket,
    and compares raw observations against that total. Its 25-page (250-record) cap covers the observed 220
    citywide records; a malformed page, failed request, origin escape, or result set that grows beyond the cap
    fails the durable run for retry instead of committing a silent partial catalog.
  - **Normalization / safety:** stable source IDs are publisher-keyed and occurrence-timestamped. The adapter
    derives a safe user handoff URL from `meta.url_path` onto `https://www.sf.gov`, never follows the legacy
    HTTP API link or an event page. It normalizes HTML descriptions/meeting overviews, physical venue/city,
    cancellation, and remote-only versus hybrid meetings. Synthetic end-of-day timestamps and invalid/end-before-
    start values become no end time. The API exposes no structured price, so every result is honestly `unknown`
    and remains discovery/handoff-only; no paid or unknown item can autonomously register.
  - **Live read-only smoke:** the first full run fetched all **220** reported public records and retained
    **169 future physical/hybrid observations / 168 canonical attachments** after policy filtering. All 169 are
    unknown price; 168 have venue text, 124 descriptions, and 167 explicit San Francisco city values. No
    account, credential, sign-in, RSVP, purchase, registration-target fetch, calendar write, or source mutation
    occurred.
  - **Coverage / verification:** offline transport tests cover three-page flattening/pacing, physical/hybrid vs
    remote/cancelled filtering, path-only handoff derivation, naive and synthetic-end-time truth, unapproved redirects,
    endpoint-shape refusal, and cap-overflow failure. Registry integration asserts the exact seed/origin/mode/cap.
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    passes (**85 unit, 19 integration; 87 source files**).
  - **NEXT:** add DataSF Our415 (official Socrata, family/youth supplement) and the reviewed South Bay RSS
    feeds as separate typed adapters; retain the current manual-refresh boundary and do not add platform crawling,
    Meetup, credentials, sign-in, RSVP, payment, or calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 8 COMPLETE: DataSF Our415 concrete occurrences.** Owner approved
  a fixed **90-day** discovery horizon for DataSF's official Our415 activity dataset, which contains both
  one-off events and bounded recurring programs. Migration `0014` adds the separately typed
  `datasf_our415` registry mode and one reviewed, anonymous City and County of San Francisco source:
  `datasf-our415-events`. Its only fetch origin is `https://data.sfgov.org`; the typed adapter accepts only
  that exact `/resource/8i3s-ih2a.json` path with no source-supplied query or pagination URL.
  - **Completeness / recurrence posture:** the adapter issues a count query followed by internally constructed,
    stable-`:id` Socrata offset pages for one fixed local-date overlap predicate. Its 25-page × 100-row raw cap
    and 10,000 concrete-occurrence cap both fail the durable run for retry instead of returning a silent partial
    catalog. It materializes publisher-declared sessions only through the inclusive 90-day local window in
    `America/Los_Angeles`: absent `days_of_week` is accepted only for a true one-day record, while a finite
    weekday grammar covers current forms such as `M-F`, `Tue-Fri`, `Sa`, and comma/slash lists. Empty,
    ambiguous, wrapping, or `TBD` schedules are skipped rather than guessed. IDs are occurrence-timestamped,
    source-keyed, and use Socrata's stable row ID only to disambiguate a duplicated publisher ID.
  - **Normalization / safety:** public `fee=false` maps to verified free and `fee=true` to paid; missing,
    malformed, or conflicting public price data stays unknown. The adapter preserves source description, venue,
    and valid coordinates without inventing a city; invalid/inverted end times become no end time. It accepts a
    direct safe HTTPS `more_info` handoff or the known bare `sfrecpark.org/register` form normalized to HTTPS,
    but never requests that handoff target. Every candidate remains discovery-only and human-handoff-only, so
    paid and unknown entries cannot autonomously register.
  - **Live read-only smoke:** the first full run read **1,912** public rows overlapping the current 90-day
    window and stored **7,510 observations / 5,707 canonical attachments**. Source observations are **2,127
    verified free, 5,356 paid, and 27 unknown**; attached canonicals have 5,699 venue values, 5,276 descriptions,
    and 5,429 coordinate pairs. No city was inferred. The concrete session range is 2026-07-17 through
    2026-10-14. All requests were paced public GETs only: no account, credential, sign-in, RSVP, purchase,
    registration-target fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline `httpx.MockTransport` tests cover count/offset query construction,
    pacing, recurrence expansion/horizon boundaries, duplicate publisher IDs, free/paid/unknown truth, HTML/geo
    mapping, invalid handoff URLs, unparseable schedules, unapproved redirects/endpoints, raw-cap, short-page,
    and occurrence-cap failures. Registry integration asserts the exact seed/origin/mode/cap. `make install &&
    make up && make migrate && make test && make test-integration && make lint typecheck` passes (**91 unit,
    19 integration; 89 source files**).
  - **NEXT:** add the reviewed South Bay RSS feed as its own typed, fixture-backed adapter; retain explicit/manual
    refresh, no platform crawling, no Meetup, and no credential, sign-in, RSVP, payment, or calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 9 COMPLETE: SCCLD Milpitas Library RSS.** Migration `0015` adds
  the separately typed `bibliocommons_rss` mode and one reviewed Santa Clara County Library District source:
  `sccld-milpitas-events`. It reads only SCCLD's anonymous location-filtered BiblioCommons RSS endpoint at
  `https://gateway.bibliocommons.com`; user handoff links are separately constrained to the publisher's
  event-detail host `https://sccl.bibliocommons.com/events/...` and are never fetched by this worker.
  - **Completeness / failure posture:** BiblioCommons exposes 25 RSS items per page but no count or next link.
    The adapter constructs only its reviewed `locations=MI` page sequence, applies a 1.5-second per-host floor,
    and stops only at a short/empty page. Its 15-page ceiling fails the durable run for retry if every page is
    full, never claiming a partial catalog. Redirects, nonmatching endpoint paths, malformed/oversize or
    entity-bearing XML, and repeated GUIDs fail the durable run before a catalog write; an unsafe individual
    event identity is rejected before it can become a candidate.
  - **Normalization / scope:** exact BiblioCommons namespace fields supply UTC start/end times, cancellation,
    virtual status, and physical location. Candidates require a matching publisher GUID/link plus `bc:id=MI`;
    cancelled, virtual-only, wrong-location, malformed, and past rows are omitted, while a future event with an
    explicit physical location remains eligible. Source-provided description, library/room, Milpitas city, and
    coordinates are retained. The feed has no structured price, so every result is honestly `unknown` and stays
    discovery/handoff-only; full registration never becomes a false cancellation or an autonomous action.
  - **Live read-only smoke:** the first run read **13** paced public RSS pages and stored **320 observations /
    320 canonical attachments**, spanning 2026-07-17 through 2027-12-14. All 320 attached canonicals have
    venue, description, coordinates, and explicit city; all 320 prices are unknown. No account, credential,
    sign-in, RSVP, purchase, registration-target fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline XML fixtures cover namespace mapping, physical/hybrid vs virtual-only,
    cancellation, location scope, matching GUID/link identity, detail-handoff validation, page construction and
    pacing, cap overflow, redirect, unsafe XML, and endpoint refusal. Registry integration asserts the exact
    seed/origin/mode/cap. `make install && make up && make migrate && make test && make test-integration &&
    make lint typecheck` passes (**96 unit, 19 integration; 91 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds; retain explicit/manual
    refresh, no platform-wide crawl, no Meetup, and no credential, sign-in, RSVP, payment, or calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 10 COMPLETE: San José City Clerk Legistar meetings.** Migration
  `0016` adds the separately typed `san_jose_legistar` mode and one reviewed City Clerk-linked Granicus
  Legistar source: `san-jose-legistar-meetings`. It fetches only the anonymous
  `https://webapi.legistar.com/v1/SanJose/Events` list endpoint; user handoff URLs are separately constrained
  to matching `https://sanjose.legistar.com/MeetingDetail.aspx?LEGID=...` records and are never fetched by
  the worker.
  - **Completeness / failure posture:** the API has no total or next cursor, so the adapter constructs its own
    fixed local **90-day** OData filter, deterministic `EventDate, EventId` ordering, 100-record pages, and
    offsets. It enforces the 1.5-second per-host floor and a five-page / 500-record ceiling. A full final page,
    repeated event id, redirect/origin escape, endpoint mismatch, HTTP/JSON/schema failure, or unsafe detail
    link fails the durable refresh for retry rather than retaining a silently truncated source. The fixed query
    never accepts user search/filter text.
  - **Normalization / scope:** publisher `EventId` plus the local occurrence timestamp forms the source-keyed
    identity. `EventDate` + strictly parsed `EventTime` are materialized in `America/Los_Angeles`; Legistar
    supplies no end time, structured price, city, or coordinates, so those are never guessed and price remains
    `unknown`. Explicitly cancelled records, virtual-only locations, blank/malformed event data, and the
    publisher's explicit `Closed Session` label are excluded; a physical location that also mentions virtual
    viewing remains eligible. Source body, comment, venue, and the safe one-tap public meeting-detail handoff
    are retained.
  - **Live read-only smoke:** `manual:san-jose-legistar-meetings:20260717-smoke-2` made one paced public GET.
    The current window returned 10 source rows and stored **2 observations / 2 canonical attachments** after
    cancellation, virtual-only, and closed-session filtering: Civil Service Commission (2026-07-20) and Appeals
    Hearing Board (2026-07-23). Both retain a venue, one retains the publisher comment, both are unknown-price,
    and neither invents a city. No account, credential, sign-in, RSVP, purchase, registration-target fetch,
    calendar write, or source mutation occurred.
  - **Coverage / verification:** offline `httpx.MockTransport` fixtures cover physical/hybrid versus virtual,
    cancelled, closed-session, past, malformed, and unsafe-handoff records; fixed OData filter/order/offset
    construction and pacing; source-page cap and repeated-id failure; unapproved redirect; and endpoint refusal.
    The registry integration asserts the exact seed/origin/mode/cap. `make install && make up && make migrate &&
    make test && make test-integration && make lint typecheck` passes (**102 unit, 19 integration; 93 source
    files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds; retain explicit/manual
    refresh, no platform-wide crawl, no Meetup, and no credential, sign-in, RSVP, payment, or calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 11 COMPLETE: Stanford official Localist occurrence API.** Migration
  `0017` promotes the existing `stanford-events` publisher record from its one-page homepage JSON-LD seed to
  Stanford's documented anonymous Localist endpoint, `https://events.stanford.edu/api/2/events`. The source key
  and publisher provenance stay stable; only the reviewed read-only fetch mechanism changes. It is separately
  typed as `localist_json`, remains handoff-only, and never follows publisher event, ticket, or registration URLs.
  - **Completeness / failure posture:** the adapter constructs its own fixed Bay Area geographic bound, local
    90-day start/end query, 100-record pages, ascending date order, and page number; it never accepts a
    source-provided next URL or user search/filter text. Localist declares the finite page count, which the
    adapter validates on every response against a 20-page ceiling. Redirect/origin/path escape, oversized or
    malformed JSON, inconsistent page metadata, an unexpectedly short non-final page, duplicate occurrence, or
    cap overflow fails the durable run for retry rather than silently returning a partial calendar. Local
    occurrence timestamps are additionally clipped to `[today, today + 90 days)` because the publisher's end
    query has inclusive-looking behavior.
  - **Normalization / scope:** Localist is occurrence-expanded, including recurring programs, so stable source
    identity is `event.id + event_instance.id`; a changed title/time does not mint a second source link. The
    adapter retains only live, public, non-rejected `inperson`/`hybrid` records with publisher coordinates inside
    the reviewed Bay Area rectangle, a physical venue, and a safe `events.stanford.edu/event/...` handoff.
    Virtual, private, rejected, malformed, past, out-of-region, and unsafe-link records are excluded. It retains
    source title, description, venue/address, city, coordinates, and actual end time when supplied. Price truth
    is deliberately conservative: `free=true` is free unless contradicted by a positive published cost; only an
    explicit positive ticket cost is paid; `free=false` without such a cost remains unknown.
  - **Live read-only smoke:** `manual:stanford-events:20260717-localist-smoke-1` performed **8** paced public
    GETs to the list API and stored **743 normalized observations / 720 canonical attachments** from the new run,
    spanning 2026-07-17 through 2026-10-14. The new observations are **99 free, 24 paid, and 620 unknown**;
    all retain venue, city, and valid coordinates, and 741 retain a description. The pre-existing ten homepage
    observations are retained as historical provenance, making 753 Stanford observations in the local catalog;
    they were neither overwritten nor deleted. No account, credential, sign-in, RSVP, purchase, event/ticket
    page fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures cover occurrence recurrence expansion and stable IDs;
    physical/hybrid/public/status/geo/horizon filtering; free/paid/unknown source truth; fixed API query and
    pacing; page-total completion and cap/duplicate failure; redirect; and endpoint refusal. The registry
    integration asserts the promoted seed/origin/mode/cap. `make install && make up && make migrate && make test
    && make test-integration && make lint typecheck` passes (**108 unit, 19 integration; 95 source files**).
  - **NEXT:** reuse the same typed Localist boundary only after separate fixture/registry review of SJSU and UCSF;
    keep every source explicit/manual, handoff-only, and free of Meetup, credentials, sign-in, RSVP, payment, or
    calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 12 COMPLETE: SJSU official Localist occurrence API.** Migration
  `0018` promotes the existing `sjsu-events` publisher record from its one-page homepage JSON-LD seed to San
  José State University's documented anonymous Localist endpoint, `https://events.sjsu.edu/api/2/events`. It
  reuses the Localist adapter only through a closed SJSU publisher specification that pins both its API and
  event-detail handoff host; a registry row cannot turn the adapter into an arbitrary Localist crawler.
  - **Completeness / normalization:** SJSU uses the same fixed Bay Area 90-day, 100-record, date-ordered page
    contract and publisher-declared page-total validation as Stanford, under the same 20-page failure cap and
    1.5-second per-host floor. Its occurrence-expanded recurring records get stable `event.id +
    event_instance.id` identities. The shared ACL retains only live, public, non-rejected `inperson`/`hybrid`
    events with a source-declared physical venue and in-bounds coordinates; virtual, private, malformed,
    out-of-region, past, and unsafe-link records are excluded. Price remains free only on an uncontradicted
    `free=true`, paid only on explicit positive cost, and otherwise unknown. Every candidate stays
    discovery/handoff-only.
  - **Live read-only smoke:** `manual:sjsu-events:20260717-localist-smoke-1` made **2** paced public API GETs
    and stored **124 normalized observations / 124 canonical attachments**, spanning 2026-07-17 through
    2026-10-14. They are **5 free, 3 paid, and 116 unknown**; all retain venue, description, city, and valid
    coordinates. The pre-existing 70 homepage observations remain retained as historical provenance, for 194
    local SJSU observations total. No account, credential, sign-in, RSVP, purchase, event/ticket page fetch,
    calendar write, or source mutation occurred.
  - **Coverage / verification:** the Localist fixture suite now proves the separate SJSU endpoint and event-link
    host are accepted while arbitrary Localist hosts remain structurally refused, alongside the existing
    occurrence, pricing, pagination, pacing, and failure coverage. The registry integration asserts the promoted
    seed/origin/mode/cap. `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**109 unit, 19 integration; 95 source files**).
  - **NEXT:** review UCSF's Localist API as a third explicit publisher specification before enabling it; retain
    the manual read-only, handoff-only boundary and no Meetup, credentials, sign-in, RSVP, payment, or calendar
    mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 13 COMPLETE: UCSF official Localist occurrence API.** Migration
  `0019` promotes the existing `ucsf-events` publisher record from its one-page homepage JSON-LD seed to UCSF's
  anonymous Localist endpoint, `https://calendar.ucsf.edu/api/2/events`. It reuses the shared adapter only through
  a closed UCSF publisher specification that pins its API and event-detail handoff host; registry data cannot turn
  it into an arbitrary Localist crawler.
  - **Completeness / normalization:** UCSF uses the same fixed Bay Area 90-day, 100-record, date-ordered page
    contract and publisher-declared page-total validation as Stanford and SJSU, under the same 20-page failure cap
    and 1.5-second per-host floor. Occurrence-expanded records retain stable `event.id + event_instance.id`
    identities. The shared ACL retains only live, public, non-rejected `inperson`/`hybrid` records with a
    source-declared physical venue and in-bounds coordinates; virtual, private, malformed, out-of-region, past,
    and unsafe-link records are excluded. Price remains free only on an uncontradicted `free=true`, paid only on
    explicit positive cost, and otherwise unknown. Every candidate remains discovery/handoff-only.
  - **Live read-only smoke:** `manual:ucsf-events:20260717-localist-smoke-1` made **1** paced public API GET and
    stored **83 normalized observations / 83 canonical attachments**, spanning 2026-07-17 through 2026-10-14.
    They are **7 free, 1 paid, and 75 unknown**; all retain venue, description, city, and valid coordinates. The
    pre-existing 15 homepage observations remain retained as historical provenance, for 98 local UCSF observations
    total. No account, credential, sign-in, RSVP, purchase, event/ticket page fetch, calendar write, or source
    mutation occurred.
  - **Coverage / verification:** the Localist fixture suite explicitly proves the separate UCSF endpoint and
    event-link host are accepted while arbitrary Localist hosts remain structurally refused, alongside the existing
    occurrence, pricing, pagination, pacing, and failure coverage. The registry integration asserts the promoted
    seed/origin/mode/cap. `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**110 unit, 19 integration; 95 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds; preserve the explicit,
    manual, read-only, handoff-only boundary and exclude Meetup, credentials, sign-in, RSVP, payment, and calendar
    mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 14 COMPLETE: Sunnyvale City Clerk Legistar meetings.** Migration
  `0020` adds one separately typed `sunnyvale_legistar` registry source,
  `sunnyvale-legistar-meetings`, for the City Clerk-linked anonymous endpoint
  `https://webapi.legistar.com/v1/SunnyvaleCA/Events`. The formerly San José-specific adapter is now a closed
  two-publisher Legistar boundary: source key, mode, exact API path, and exact matching public meeting-detail host
  must agree, so registry data cannot turn it into an arbitrary Granicus client. San José retains its existing
  source identities and reviewed behavior.
  - **Completeness / normalization:** the adapter constructs the fixed local 90-day OData filter, deterministic
    `EventDate, EventId` ordering, 100-record pages, and offsets; it applies the 1.5-second shared API-host floor
    and fails the durable run on cap truncation, repeated publisher id, redirect/origin escape, malformed page, or
    unsafe handoff. Sunnyvale-specific `Hidden` agenda status, explicit cancellation, closed session, virtual-only,
    malformed, and past records are excluded; an `Online and Council Chambers` hybrid venue is retained. The stable
    source identity is `EventId + local occurrence timestamp`; the API provides no structured price, city,
    coordinates, or end time, so price remains unknown and those fields are never invented. Every retained detail
    URL is a matching `https://sunnyvaleca.legistar.com/MeetingDetail.aspx?LEGID=...` handoff and is never fetched.
  - **Live read-only smoke:** `manual:sunnyvale-legistar-meetings:20260717-smoke-1` made **1** paced public API
    GET and stored **3 observations / 3 canonical attachments**, all unknown-price, with a venue and description
    but no invented city. The current candidates are Sustainability Commission (2026-07-20 19:00), City Council
    (2026-07-21 17:30), and Housing and Human Services Commission (2026-07-22 19:00), all local Pacific time.
    No account, credential, sign-in, RSVP, purchase, meeting-detail fetch, calendar write, or source mutation
    occurred.
  - **Coverage / verification:** offline fixtures prove the exact Sunnyvale API and handoff host, hybrid retention,
    and hidden/cancelled/virtual/past/unsafe-handoff exclusion; they also prove an unreviewed source key makes zero
    requests. Existing Legistar coverage continues to prove fixed pagination, pacing, cap/repetition failure, and
    redirect refusal for the shared adapter. The registry integration asserts the exact new seed/origin/mode/cap.
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck` passes
    (**112 unit, 19 integration; 95 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds; preserve the explicit,
    manual, read-only, handoff-only boundary and exclude Meetup, credentials, sign-in, RSVP, payment, and calendar
    mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 15 COMPLETE: Berkeley Public Library Communico occurrence API.**
  Migration `0021` adds one separately typed `communico_json` registry source,
  `berkeley-public-library-events`, for Berkeley Public Library's anonymous
  `https://berkeleypubliclibrary.libnet.info/eeventcaldata` list endpoint. The dedicated adapter is closed to the
  exact source key, host, and path; it cannot be repurposed by registry data into an arbitrary Communico client.
  It never follows the source `url`, registration, or third-party-link fields, and instead constructs only the
  matching official `/event/{id}` human handoff from a positive publisher id.
  - **Completeness / normalization:** the adapter owns one fixed Los Angeles-local request with
    `private=false`, local start date, `days=91`, and `event_type=0`, then clips source occurrences locally to the
    half-open 90-day horizon. The endpoint is unpaged, so a response at its 600-row ceiling, a body over 2 MB,
    redirect/origin/path escape, malformed JSON/object, or bad record fails the durable run rather than claiming a
    partial catalog. It retains only explicit `INPERSON`/`HYBRID`, non-private, non-cancelled records with an
    actual source library/location. Stable identity is publisher id plus local occurrence timestamp, preserving
    distinct recurring times; exact duplicate rows collapse safely. It keeps source-provided end times, including
    an all-day record's explicit 23:59 end, but does not invent city, coordinates, or price; the ambiguous
    registration-cost field remains `unknown`.
  - **Live read-only smoke:** `manual:berkeley-public-library-events:20260717-communico-smoke-1` made **1** paced
    public GET and stored **453 observations / 434 canonical attachments**. The resulting local range is
    2026-07-17 10:30 through 2026-10-14 17:30 Pacific; all 453 retain a physical venue and source end time, 444
    retain a description, and all remain unknown-price with no invented city. Example retained occurrences include
    Bilingual Baby Bounce, Botany with East Bay Parks, Community Craft Circle, and Super Cinema. No account,
    credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove the exact fixed request, local horizon, official handoff
    construction, physical/hybrid retention, all-day source end time, unknown-price policy, duplicate collapse, and
    virtual/private/cancelled/past/out-of-horizon/malformed/no-venue exclusion. They also prove capped unpaged data
    fails and an unreviewed source key makes zero requests. The registry integration asserts the exact new
    seed/origin/mode/cap. `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**115 unit, 19 integration; 97 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 16 COMPLETE: SCCLD Saratoga Library RSS.** Migration `0022` adds
  `sccld-saratoga-events`, a second separately reviewed Santa Clara County Library District source at the
  anonymous location-filtered BiblioCommons RSS endpoint
  `https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA`. The shared adapter is now a
  closed two-location map: source key, exact `locations` query value, and matching SCCLD detail host must agree,
  so a registry record cannot make it crawl another library. Existing Milpitas behavior and source identity remain
  stable.
  - **Completeness / normalization:** SCCLD exposes 25 RSS items per page without a count or next cursor. The
    adapter constructs only its reviewed location query and positive page sequence, applies the 1.5-second shared
    host floor, and fails a 15-page full-cap, repeated GUID, redirect/origin/path/query escape, malformed/oversize
    or entity-bearing XML, or unsafe event handoff rather than claiming a partial source. It retains only matching
    `bc:id=SA`, non-cancelled physical/hybrid records with paired official GUID/link handoffs. Source city,
    coordinates, venue/room, description, and UTC end time are retained; the feed has no structured price, so all
    remain unknown-price. It intentionally preserves the pre-existing Milpitas policy of retaining future
    publisher occurrences rather than introducing a new unratified per-source horizon.
  - **Live read-only smoke:** `manual:sccld-saratoga-events:20260717-rss-smoke-1` made **14** paced public RSS
    GETs and stored **332 observations / 332 canonical attachments**, from 2026-07-17 through 2028-12-28. Every
    attached canonical has a publisher venue, Saratoga city, coordinates, description, and end time; all 332 are
    unknown-price. Sample current records include Indian Instrumental Duet Concert, The Asian American Struggle
    for Belonging, Mandarin Storytime, and community art exhibits. No account, credential, sign-in, RSVP,
    purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove exact Saratoga feed/physical-location acceptance, hybrid
    retention, wrong-location and virtual-only exclusion, official handoff normalization, and unreviewed-source
    refusal. Existing BiblioCommons coverage continues to prove XML safety, pagination/pacing, cap overflow,
    redirect refusal, and the stable Milpitas contract. The registry integration asserts the new seed/origin/mode/
    cap. `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    passes (**117 unit, 19 integration; 97 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 17 COMPLETE: Oakland Museum of California official event-list API.**
  Migration `0023` adds the separately typed `tribe_events_json` source `omca-events`, pinned to the museum's
  anonymous `https://museumca.org/wp-json/tribe/events/v1/events` list API. The adapter is a closed OMCA
  source-key/host/path boundary, not a generic WordPress or The Events Calendar crawler; it never follows the
  returned `rest_url`, website, ticket, virtual, or event-detail URLs.
  - **Completeness / normalization:** the adapter owns the fixed Los Angeles-local 90-day `start_date`/inclusive
    `end_date`, `per_page=50`, `page`, and `status=publish` query, then clips locally to the half-open horizon.
    It validates stable publisher `total`/`total_pages` metadata under a five-page / 250-record cap, exact page
    completion, response size, redirects, and duplicate occurrence identity. A candidate requires publisher
    `publish` status, `hide_from_listings=false`, `is_virtual=false`, `all_day=false`, exact
    `America/Los_Angeles` time zone, a named source venue, matching local/UTC start and end timestamps, and a safe
    exact-host `/event/...` handoff. It retains no invented city or coordinates. Exact unqualified `Free` is free,
    an explicit positive cost is paid, and member-restricted/ambiguous offers remain unknown; all stay
    discovery/handoff-only.
  - **Live read-only smoke:** `manual:omca-events:20260717-tribe-smoke-1` made **1** paced public API GET and
    stored **13 observations / 13 canonical attachments**, spanning 2026-07-17 through 2026-10-04. They are
    **10 free, 1 paid, and 2 unknown**; all retain a named venue, bounded source-list excerpt, and source end
    time, with no inferred city/coordinates. The current set includes Friday Nights at OMCA programs, an
    Architecture Walk and Talk, and a paid Spotlight Sunday conversation. No account, credential, sign-in, RSVP,
    purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove the fixed query and local/UTC timestamp cross-check,
    physical timed/public filtering, safe event handoff, free/paid/unknown truth, horizon exclusion, declared
    pagination/pacing, cap failure, and unreviewed-source refusal. The registry integration asserts the exact
    seed/origin/mode/cap. `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**121 unit, 19 integration; 99 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 18 COMPLETE: Palo Alto City Library official RSS feed.** Migration
  `0024` adds `palo-alto-library-events`, a separately reviewed, anonymous, handoff-only Palo Alto City Library
  source at `https://gateway.bibliocommons.com/v2/libraries/paloalto/rss/events`. It reuses the existing
  `bibliocommons_rss` mode, but the shared adapter now holds a closed per-publisher profile (exact gateway path,
  static query, official handoff host, location policy, and optional source-specific date window); registry data
  cannot turn it into a generic BiblioCommons crawler. SCCLD Milpitas and Saratoga retain their exact static
  `locations=MI|SA` requests and intentionally unbounded future-occurrence policy.
  - **Completeness / normalization:** Palo Alto's adapter owns the fixed Los Angeles-local 90-day
    `startDate`/inclusive-`endDate` request and positive page sequence, then retains only the half-open local
    horizon. It requires matching official GUID/link handoffs at `https://paloalto.bibliocommons.com/events/...`,
    non-cancelled future physical/hybrid occurrences, and one of nine reviewed concrete source location ids
    (Children's, Downtown, Mitchell Park, Rinconada, and College Terrace Libraries plus the observed named
    community-center sites). This explicitly excludes the publisher's non-venue `All Branches` closure placeholder,
    virtual-only, missing/unknown-location, unsafe, malformed, repeated, and out-of-window rows. Source UTC end
    time, city, coordinates, venue/room, and sanitized description are retained; the feed has no structured price,
    so all remain `unknown`. The endpoint has 25 RSS rows per page and no count/next cursor: redirects, endpoint or
    query escape, unsafe XML, oversize responses, duplicate GUIDs, or a full 15-page reviewed cap fail the durable
    refresh rather than claim partial coverage.
  - **Live read-only smoke:** `manual:palo-alto-library-events:20260717-rss-smoke-1` made **13** paced anonymous
    RSS GETs and stored **278 observations / 260 canonical attachments**, from 2026-07-17 10:30 through
    2026-10-14 18:30 Pacific. All 278 retain a source venue, Palo Alto city, coordinates, description, source end
    time, and unknown price. Sample records include Family Storytime, Learn English: Beyond the Basics, LEGO
    Fridays!, Meditation with Sara, Mindfulness Meditation, and Happy Birds. No account, credential, sign-in,
    RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove the exact adapter-owned local request, official handoff,
    reviewed physical-location allowlist, hybrid retention, virtual/cancelled/placeholder/past exclusion, inclusive
    endpoint boundary clip, pagination/pacing, and registry-query refusal. Existing BiblioCommons tests continue
    to prove XML safety, cap overflow, redirect refusal, and stable SCCLD contracts. The registry integration
    asserts the exact new seed/origin/mode/cap. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**124 unit, 19 integration; 99 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 19 COMPLETE: San Mateo County Libraries Millbrae official RSS feed.**
  Migration `0025` adds `smcl-millbrae-events`, a separately reviewed, anonymous, handoff-only Millbrae source at
  `https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?locations=1M`. It reuses the closed
  BiblioCommons publisher map with its own exact library path, `locations=1M` query, Millbrae-only physical
  location id, official `https://smcl.bibliocommons.com/events/...` handoff host, and source-specific local-date
  window; it cannot expand the adapter into a general San Mateo County Libraries crawler.
  - **Completeness / normalization:** the adapter adds only its fixed Los Angeles-local 90-day
    `startDate`/inclusive-`endDate` request and positive pages to the reviewed location query, then clips source
    occurrences locally to the half-open horizon. It retains only matching official GUID/link rows at concrete
    `bc:id=1M`, non-cancelled physical/hybrid future occurrences. The list endpoint has 25 RSS rows per page and no
    count or next cursor: a redirect, endpoint/query escape, malformed or entity-bearing XML, oversize response,
    repeated GUID, or full 15-page cap fails the durable refresh instead of retaining partial coverage. It preserves
    the source venue/room, Millbrae city, coordinates, description, and UTC end time; no structured price exists,
    so every item is `unknown` rather than inferred from its prose.
  - **Live read-only smoke:** `manual:smcl-millbrae-events:20260717-rss-smoke-1` made **10** paced anonymous RSS
    GETs and stored **223 observations / 223 canonical attachments**, from 2026-07-17 10:00 through
    2026-10-14 15:30 Pacific. All 223 retain source venue, Millbrae city, coordinates, description, source end
    time, and unknown price. Sample records include Open Sewing, Tai Chi, Beginner Cantonese Club, Maker
    Exploration, the Friends of the Library Outdoor Bargain Book Sale, and Digital Help Hub. No account,
    credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove the exact location-plus-date request, official handoff,
    physical/hybrid retention, wrong-location/virtual-only/cancelled/end-boundary exclusion, unknown-price policy,
    and registry-supplied date-query refusal. Existing adapter coverage continues to prove pagination/pacing,
    cap-failure, redirect refusal, XML safety, and the unchanged SCCLD/Palo Alto profiles. The registry integration
    asserts the exact seed/origin/mode/cap. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**126 unit, 19 integration; 99 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 20 COMPLETE: Mountain View Public Library official LibCal ICS feed.**
  Migration `0026` adds the separately typed `libcal_ics` registry source `mountain-view-library-events`, pinned to
  the library's anonymous `https://mountainview.libcal.com/ical_subscribe.php?src=p&cid=8800` calendar feed. The
  new adapter is a closed Mountain View profile (exact host/path/query, `LibCal-8800-<positive id>` UID prefix,
  matching official detail host, and physical-location allowlist), not a generic iCalendar or LibCal crawler.
  Composition wires it only through the existing catalog-fetcher port; no credentials, source-specific registration
  behavior, or cloud dependency was introduced.
  - **Completeness / normalization:** the parser accepts only a bounded UTF-8 VCALENDAR/VEVENT subset (1 MB,
    fewer than 300 VEVENTs), unfolds RFC 5545 text safely, rejects malformed envelopes/nested components, duplicate
    singleton fields or UIDs, and any recurrence construct rather than claiming a partial materialized calendar. It
    accepts only publisher UTC timed start/end values, clips to the Los Angeles-local half-open 90-day horizon, and
    rejects all-day/floating/TZID timestamps instead of guessing. A candidate requires a safe source `URL` that
    exactly agrees with its validated UID, but the worker constructs the human handoff itself and never follows that
    URL. It retains only the eight reviewed concrete source locations (program rooms, History Center, Pioneer Park,
    bike station, Bookmobile Garage, Children's Room, and Teen Zone), excluding `Online`, `Offsite`, blank/unknown
    locations, cancelled `STATUS`, and explicit `canceled`/`cancelled` titles. Source text is unescaped, sanitized,
    and description-capped; source city, coordinates, and a structured price do not exist, so they remain absent or
    `unknown` rather than inferred.
  - **Live read-only smoke:** the final corrected run,
    `manual:mountain-view-library-events:20260717-ics-smoke-2`, made **1** paced anonymous calendar GET and stored
    **84 observations / 84 canonical attachments**, from 2026-07-17 14:00 through 2026-10-14 18:30 Pacific. All 84
    retain a publisher venue and source end time; 80 retain a description, all are unknown-price, and none invent a
    city or coordinate. Explicitly cancelled source-title rows were excluded. Current examples include STEAM
    Fridays, Drop-in Bike Clinic, Toddler Drive In, the San Francisco Dance Film Festival, Create a Mini Library,
    Story Stones, and Summer Outdoor Storytime. No account, credential, sign-in, RSVP, purchase, event-detail
    fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove exact endpoint closure/redirect refusal, UTF-8 and envelope
    safety, line unfolding/escaping, strict UID+URL-to-handoff agreement, physical-location and cancellation policy,
    all-day/floating/out-of-window exclusion, source count/recurrence/duplicate failure, and per-host pacing. The
    registry integration asserts the exact seed/origin/mode/cap. `make install && make up && make migrate && make
    test && make test-integration && make lint typecheck` passes (**130 unit, 19 integration; 101 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 21 COMPLETE: San Francisco Recreation & Parks Main Calendar RSS.**
  Migration `0027` adds the separately typed `sf_rec_park_rss` source `sf-rec-park-events`, pinned to San
  Francisco Recreation & Park Department's expressly published, anonymous
  `https://sfrecpark.org/RSSFeed.aspx?CID=Main-Calendar-14&ModID=58` Main Calendar feed. The dedicated adapter is
  a closed source-key/host/path/query profile, not a generic CivicPlus crawler; it never follows the returned
  event-detail, enclosure, ticket, or registration URLs. The official RSS directory publishes this feed for RSS
  readers, and the site's `robots.txt` permits `RSSFeed.aspx` under its general policy.
  - **Completeness / normalization:** the unpaged feed is deliberately modeled as a near-term rolling supplement,
    not as 90-day coverage: it currently exposes 15 days and publishes no cursor, total, or next link. One
    document is bounded at 1 MB and fewer than 200 items; a cap hit, duplicate occurrence GUID, redirect/origin or
    query escape, malformed RSS root/channel, or DTD/entity-bearing XML fails the durable refresh rather than
    claiming a partial rolling window. A candidate requires one exact same-host
    `Calendar.aspx?EID=<positive-id>` handoff and a matching non-permalink GUID suffix, so the stable identity is
    publisher `EID + occurrence ticks`. It takes occurrence day and paired AM/PM times only from the namespaced
    source fields in `America/Los_Angeles` (never `pubDate`), rejects malformed/all-day/past/end-before-start rows,
    and retains only named locations explicitly ending in `San Francisco, CA`; title-level cancelled/postponed rows
    and virtual-only locations are excluded. Source venue, city, end time, and bounded sanitized description are
    retained; no geo or structured price exists, so geo remains absent and every price is `unknown` rather than
    inferred.
  - **Live read-only smoke:** `manual:sf-rec-park-events:20260717-rss-smoke-1` made **1** paced public RSS GET
    and stored **32 observations / 32 canonical attachments**, from 2026-07-17 11:30 through 2026-07-31 17:00
    Pacific. All 32 retain a source venue, explicit San Francisco city, source end time, description, and
    unknown price. Current examples include Friday Chess Lessons, Golden Gate Bandshell Friday Happy Hour, Union
    Square Dances, Third Saturday Karaoke, and Crucial Reggae Sunday. No account, credential, sign-in, RSVP,
    purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove exact endpoint closure, source-built official handoff and
    GUID agreement, local date/time normalization, physical/cancelled/virtual/past/malformed/unsafe exclusion,
    XML safety, duplicate/cap failure, per-host pacing, redirect refusal, and unreviewed or registry-tampered
    source refusal. The registry integration asserts the exact new seed/origin/mode/cap. `make install && make up
    && make migrate && make test && make test-integration && make lint typecheck` passes (**134 unit, 19
    integration; 103 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 22 COMPLETE: Gardens of Golden Gate Park official event-list API.**
  Migration `0028` adds `gardens-golden-gate-park-events` under the existing typed `tribe_events_json` mode,
  pinned to the Gardens of Golden Gate Park's anonymous
  `https://gggp.org/wp-json/tribe/events/v1/events` public event list. The shared adapter's closed publisher map
  now requires this exact source key, host, path, and `https://gggp.org/event/...` human-handoff host, so the
  registry cannot turn it into a generic WordPress or The Events Calendar crawl. It never follows the returned
  REST/detail, ticket, website, or event URLs.
  - **Completeness / normalization:** the adapter owns the fixed Los Angeles-local 90-day query (`per_page=50`,
    `page`, `status=publish`) and clips rows to the half-open local horizon. Publisher-declared `total` and
    `total_pages` must stay stable under the reviewed five-page / 250-row ceiling; response-size, redirect,
    source-key/path, pagination, duplicate-occurrence, malformed, and unsafe-handoff failures are retryable rather
    than partial. It retains only `publish`, visible, non-virtual, timed, exact-`America/Los_Angeles` rows with a
    named source venue, matching local/UTC start and end timestamps, and a safe official `/event/...` handoff;
    malformed `UTC+0` and no-venue rows are excluded rather than corrected or inferred. No city or coordinates are
    invented. The shared structured-price parser now recognizes positive numeric `cost_details.values` ranges such
    as `$55–$75` as `paid`, while exact `Free` remains free and ambiguous offers remain unknown; paid rows stay
    discovery/handoff-only.
  - **Live read-only smoke:** `manual:gardens-golden-gate-park-events:20260717-tribe-smoke-1` made **2** paced
    public API GETs for the publisher-declared 52 rows and stored **46 observations / 46 canonical attachments**.
    The retained local range is 2026-07-17 10:00 through 2026-10-11 08:30 Pacific; all 46 have a named source
    venue and source end time, 43 retain a bounded description, and the price split is **24 free / 22 paid**.
    Current examples include Bean Sprouts Family Days, Ask A Master Gardener, Essential Oil + Tea Blending Workshop,
    Midday Yoga in the Garden, and Birding at the Garden. No account, credential, sign-in, RSVP, purchase,
    event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** separate offline fixtures prove the exact Gardens profile and local request,
    official handoff, strict physical/time-zone filtering, and structured positive price-range classification, plus
    source-key/seed tampering refusal. Existing The Events Calendar tests continue to cover finite pagination,
    pacing, cap failure, redirect refusal, source truth, and OMCA compatibility. The registry integration asserts
    the exact new seed/origin/mode/cap. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**136 unit, 19 integration; 103 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 23 COMPLETE: Alameda County Library Fremont official RSS feed.**
  Migration `0029` adds `alameda-county-library-fremont-events`, a separately reviewed, anonymous,
  handoff-only Fremont source at
  `https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?locations=FRM`. It reuses the closed
  BiblioCommons publisher map with an Alameda County Library-specific gateway path, Fremont-only location query
  and physical location id, matching `https://aclibrary.bibliocommons.com/events/...` handoff host, and an
  adapter-owned local-date window; registry data cannot make it crawl another branch or library.
  - **Completeness / normalization:** the feed has 25 RSS rows per page but no count or next cursor. The adapter
    constructs only the fixed `locations=FRM`, Los Angeles-local 90-day `startDate`/inclusive-`endDate`, and
    positive page sequence, then clips records to the half-open local horizon. The observed window has 141 raw
    rows over six pages; the reviewed seven-page ceiling fails if all seven pages are full instead of silently
    retaining a partial catalog. It requires matching official GUID/link handoffs, exact `bc:id=FRM`, a named
    physical source location, and future non-cancelled physical/hybrid rows. Redirect, endpoint/query escape,
    malformed/entity-bearing XML, oversize response, repeated GUID, or cap overflow is retryable. Source
    venue/room, Fremont city, coordinates, UTC end time, and sanitized description are retained; the feed has no
    structured cost, so every price remains `unknown` rather than inferred.
  - **Live read-only smoke:** `manual:alameda-county-library-fremont-events:20260717-rss-smoke-1` made **6**
    paced anonymous RSS GETs and stored **137 observations / 135 canonical attachments**, from 2026-07-17 13:00
    through 2026-10-14 13:30 Pacific. Every observation retains a source venue, Fremont city, coordinates,
    description, end time, and unknown price. Current examples include Show Me Your Magic, Alameda County Child
    Support Services Tabling, Bricks & Beverages, Learn to Crochet, and Let's Learn Mandarin. No account,
    credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove the exact Fremont location-plus-date request, official
    handoff, physical/hybrid retention, virtual/cancelled/wrong-location/end-boundary exclusion, source spatial
    fields, unknown-price policy, and registry-supplied date-query refusal. Existing shared RSS coverage continues
    to prove XML safety, pagination/pacing, cap failure, redirect refusal, and stable older publisher profiles. The
    registry integration asserts the exact new seed/origin/mode/cap. `make install && make up && make migrate &&
    make test && make test-integration && make lint typecheck` passes (**138 unit, 19 integration; 103 source
    files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 24 COMPLETE: Midpeninsula Regional Open Space District Events &
  Activities.** Migrations `0030` and `0031` add the separately typed `midpen_html` source `midpen-events`,
  pinned to Midpen's anonymous, server-rendered
  `https://www.openspace.org/get-involved/events-activities?page=0` list. The dedicated closed adapter accepts
  only that host, path, and one constructed nonnegative `page` query under its reviewed nine-page cap; registry
  data cannot turn it into a generic Drupal crawl. It creates only safe same-publisher `/events/...` handoffs and
  never follows the event, volunteer, registration, or any external URL.
  - **Completeness / normalization:** the source has a 15-row compact sliding pager rather than a global numeric
    page count. The adapter validates each active page, every numeric link, and the immediately adjacent safe
    successor, then traverses pages `0` through `8` at the source cadence. It permits Midpen's verified inactive
    visual Next control only when the exact numeric successor is present, and recognizes the verified empty-href
    final Next control only when no later numeric page exists; unsafe, skipped, out-of-cap, malformed, missing,
    or short-nonfinal pagination fails the durable refresh rather than retaining a partial list. Exact normalized
    list overlaps collapse idempotently; a same path/start occurrence with conflicting source fields fails closed.
    Candidates require a future timed source row, a safe event handoff, and one of the reviewed named physical
    Midpen preserves; cancelled/postponed, online, meeting, unnamed, past, malformed-time, and unsafe-link rows
    are excluded. Source preserve, activity type/subtype, and distance form the bounded description. The list
    provides neither an end time, city, coordinates, nor structured price, so those fields remain absent and every
    price remains `unknown` rather than inferred.
  - **Live read-only smoke:** the successful durable retry
    `manual:midpen-events:20260717-html-smoke-1` made **9** paced anonymous public-list GETs and stored **85
    observations / 85 canonical attachments**, from 2026-07-17 10:00 through 2026-11-28 10:00 Pacific. All 85
    retain a source preserve and bounded description; none invent an end time, city, coordinate, or price. Current
    examples include Adaptations at the Marsh, Habitat Restoration: Yellow Star Thistle Removal, Trail Maintenance:
    South Leaf Trail (Pt. 2), Moth Night, and Ramble at Rancho. No account, credential, sign-in, RSVP, purchase,
    event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** offline fixtures prove exact registry/endpoint closure, redirect refusal, table
    shape, local time normalization, reviewed-preserve and title-policy filtering, safe
    handoff construction, per-host pacing, compact pager / inactive-Next / empty-final-control traversal, cap and
    inconsistent-pager failure, and exact-overlap versus conflicting-occurrence behavior. The registry integration
    asserts the exact seed/origin/mode/nine-page cap. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**145 unit, 19 integration; 105 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 25 COMPLETE: closed CivicEngage RSS adapter plus Campbell
  Recreation & Community Services.** Migration `0032` promotes the existing SF Recreation & Parks registry row
  from its narrow `sf_rec_park_rss` type to `civic_engage_rss`, preserving its stable `sf-rec-park` source-event
  IDs, and adds the separately reviewed `campbell-events` source pinned to the City of Campbell's anonymous
  `https://www.campbellca.gov/RSSFeed.aspx?CID=Recreation-Community-Services-29&ModID=58` calendar feed. The
  shared adapter is still a closed publisher map—not a platform crawler: each profile requires its exact source
  key, HTTPS host, path, ordered query, RSS namespace, safe `Calendar.aspx?EID=<positive-id>` handoff, GUID
  occurrence agreement, locality suffix, source ID prefix, item ceiling, and one-document registry cap. It never
  follows an event-detail, ticket, registration, enclosure, or external URL. Campbell's `robots.txt` permits this
  exact `/RSSFeed.aspx` path; its separate `/RSS.aspx` directory was not used.
  - **Completeness / normalization:** both CivicEngage feeds are unpaged rolling windows, so the adapter makes one
    paced list GET and fails at the profile's reviewed ceiling (200 SF items; 50 Campbell items) rather than
    claiming a partial calendar. It parses only namespaced `EventDates`, paired local `EventTimes`, `Location`,
    title, description, link, and non-permalink GUID; `pubDate` and title words such as “Free” never determine an
    occurrence or price. SF remains strict about a named San Francisco location. Campbell accepts only locations
    explicitly ending in `Campbell, CA 95008`: a source-provided prefix becomes a sanitized venue, while an exact
    city-only value remains a physical Campbell event with `venue_name=None` rather than a guessed venue or an
    event-detail lookup. Virtual-only, cancelled/postponed, malformed, past, wrong-locality, unsafe-handoff, and
    duplicate rows are excluded or fail closed as appropriate. Source end times, Campbell city, and bounded
    descriptions are retained; neither feed provides coordinates or structured pricing, so geo remains absent and
    price is always `unknown`.
  - **Live read-only smoke:** `manual:campbell-events:20260717-rss-smoke-1` made **1** paced anonymous public RSS
    GET and stored **11 observations / 11 canonical attachments**, from 2026-07-17 11:00 through 2026-07-30 18:30
    Pacific. All 11 retain Campbell city, source end time, description, and unknown price; **7** retain a named
    source venue and **4** retain the publisher's explicit city-only location without an invented venue. Current
    examples include Family Fun at the Museum, Free Day at the Museum, Yoga at the Orchard City Green, Free Movie
    Night – Zootopia 2 (PG), Adaptive Dis-Glow Dance, and Summer Concert Series – Get Lucky. No account,
    credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** the existing SF profile fixtures now exercise the generic adapter and preserve SF
    identity behavior. Campbell fixtures prove exact profile/query ordering, source namespace and GUID/link
    agreement, city-only retention, HTML `<br>` location cleanup, local time/end normalization, free-in-title
    unknown pricing, virtual/cancelled/past/wrong-city/unsafe exclusion, per-host pacing, duplicate and 50-item
    cap failure, XML/redirect refusal, and seed/cap tampering refusal. Registry integration asserts both exact
    CivicEngage seed/origin/mode/cap records. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**148 unit, 19 integration; 107 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 26 COMPLETE: City of Los Altos official all-calendar RSS feed.**
  Migration `0033` adds the separately reviewed `los-altos-events` profile under the closed shared
  `civic_engage_rss` mode, pinned to
  `https://www.losaltosca.gov/RSSFeed.aspx?CID=All-calendar.xml&ModID=58`. The profile accepts only that exact
  `www.losaltosca.gov` host, path, ordered query, XML namespace, one-document cap, and safe same-host
  `Calendar.aspx?EID=<positive-id>` handoff with a matching non-permalink GUID occurrence suffix. It never
  follows the handoff or any event, ticket, registration, enclosure, or external URL. The city's general
  `robots.txt` rule disallows `/RSS.aspx`, not this expressly published `/RSSFeed.aspx` calendar path.
  - **Completeness / normalization:** the feed is an unpaged near-term rolling document with no cursor, total, or
    next link, so one paced GET is bounded at a reviewed 50-item fail-closed ceiling. It takes only namespaced
    `EventDates` and paired local `EventTimes` (not the feed's stale-offset `pubDate`) and retains a future,
    non-cancelled source row only when its explicit location has a nonempty physical prefix followed by the
    reviewed `Los Altos, CA 94022` or `94024` suffix. Virtual, city-only, wrong-locality, malformed, past, unsafe,
    and duplicate rows are excluded or fail closed as appropriate. Escaped `<br>` text and only an adjacent,
    repeated source locality (including its dangling separator) are removed from a venue; no address or city is
    inferred. Source venue, Los Altos city, end time, and bounded description are retained; the feed supplies no
    coordinates or structured offer, so geo stays absent and price remains `unknown`.
  - **Live read-only smoke:** `manual:los-altos-events:20260717-rss-smoke-1` made **1** paced anonymous public RSS
    GET and stored **5 observations / 5 canonical attachments**, from 2026-07-20 18:00 through 2026-07-30 12:00
    Pacific. All five retain a source venue, Los Altos city, end time, description, and unknown price; none invents
    coordinates. Current examples include Financial Commission Meeting, Downtown Park Outreach – Los Altos Library,
    Downtown Park Outreach – Woodland Library, City Council Regular Meeting, and Santa Clara County Fire Community
    Outreach Session. No account, credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or
    source mutation occurred.
  - **Coverage / verification:** offline fixtures prove exact profile/query ordering, namespace and GUID/link
    identity, recurrence-safe IDs, local time/end normalization, named-location and repeated-locality cleanup,
    free-in-title unknown pricing, virtual/city-only/cancelled/past/wrong-city/unsafe exclusion, duplicate and
    50-item cap failure, XML/redirect refusal, and registry cap/query tampering refusal. Registry integration
    asserts the exact seed/origin/mode/cap. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**151 unit, 19 integration; 107 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 27 COMPLETE: Oakland Public Library official RSS calendar.**
  Migration `0034` adds the separately reviewed `oakland-public-library-events` profile under the existing closed
  `bibliocommons_rss` mode. It is pinned to BiblioCommons's anonymous gateway endpoint for Oakland Library and to
  one exact, ordered, repeated `locations` query containing 24 reviewed physical Oakland locations, including the
  publisher-declared Coliseum, Ira Jinkins Community Center, and Fremont High School locations. The adapter accepts
  only that HTTPS gateway host/path/query, then creates only matching GUID/link handoffs on
  `oaklandlibrary.bibliocommons.com/events/...`; it never follows a handoff, event-detail, ticket, registration,
  or external URL. The generic adapter now also binds each reviewed BiblioCommons profile's static page ceiling to
  its registry row, so registry data cannot silently widen an existing source crawl.
  - **Completeness / normalization:** the closed query uses BiblioCommons's verified OR semantics over the 24
    locations and adds a local Pacific 90-day `startDate`/`endDate` window only at request time. The observed
    window has 46 full 25-row pages and a 15-row page 47; a source-bound 60-page ceiling leaves bounded headroom
    and fails the durable refresh if all 60 pages are full. It retains only future, non-cancelled physical or hybrid
    rows with a reviewed location ID, a matching safe GUID/link handoff, valid UTC start time, and an occurrence
    inside the half-open local horizon. Virtual-only, remote/wrong-location, cancelled, malformed, past,
    out-of-horizon, unsafe, and mismatched-identity rows are excluded. Source venue, city, coordinates, end time,
    and sanitized description are retained; the feed has no structured price, so price remains `unknown` rather
    than inferred. BiblioCommons permits RSS/XML automation, but its service-content terms include a
    personal/non-commercial clause; this source should receive owner/legal review before commercial redistribution.
  - **Live read-only smoke:** `manual:oakland-public-library-events:20260717-rss-smoke-1` made **47** paced
    anonymous public RSS list GETs and stored **1,112 candidates / 1,110 canonical attachments** in one durable
    attempt. The canonical window is 2026-07-17 12:00 through 2026-10-14 17:30 Pacific. All 1,110 canonical
    events retain a source venue, Oakland city, coordinates, end time, and description; every price is `unknown`.
    Current examples include Bay Area Discovery Museum's Try It Truck, Lunchtime Jazz Concert at Main Library,
    Ventriloquist! - Marc Griffiths, Drawing with Alan Leon, Teen Scape, and The Bike Fix. No account, credential,
    sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** Oakland fixtures prove exact duplicate-query ordering, all-location allowlisting,
    local-window clipping, physical/hybrid retention, safe GUID/link agreement, virtual/cancelled/past/wrong-
    location/unsafe exclusion, pacing, short-page termination, tampered query/cap refusal, and 60-page overflow
    failure. Registry integration asserts the exact seed/origin/mode/cap. `make install && make up && make migrate
    && make test && make test-integration && make lint typecheck` passes (**155 unit, 19 integration; 107 source
    files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 28 COMPLETE: San José Public Library official RSS calendar.**
  Migrations `0035` and `0036` add the separately reviewed `san-jose-public-library-events` profile under the
  closed `bibliocommons_rss` mode, pinned to SJPL's anonymous gateway endpoint and to an exact ordered repeated
  `locations` query for its 25 named physical branches. The profile accepts only that HTTPS gateway host/path/query
  and matching `sjpl.bibliocommons.com/events/...` GUID/link handoffs; it never follows a handoff, event detail,
  ticket, registration, or external URL. The worker excludes BiblioCommons's virtual location and requires an
  actual publisher-provided physical location, so virtual-only events cannot enter through a selected branch ID.
  - **Completeness / calibration:** the public UI reports 3,997 events for the same closed physical-branch set
    across all dates. With 25 RSS rows per page, migration `0036` binds a 160-page source ceiling that covers that
    broader set; the narrower Pacific 90-day request adds `startDate`/`endDate` only at request time and stops at
    its first short page. The initial 60-page probe deliberately failed closed before writing an observation or
    canonical event, proving the source cannot silently retain a partial window; the calibrated retry completed
    within the 160-page limit. It retains only future, non-cancelled physical or hybrid rows with a reviewed
    location ID, matching safe GUID/link identity, valid UTC start, and an occurrence inside the half-open local
    horizon. Virtual-only, wrong-location, cancelled, malformed, past, out-of-horizon, unsafe, and
    mismatched-identity rows are excluded. Source venue, city, coordinates, end time, and sanitized description
    are retained; absent structured price remains `unknown`. BiblioCommons permits RSS/XML automation, but its
    service-content terms include a personal/non-commercial clause; this source should receive owner/legal review
    before commercial redistribution.
  - **Live read-only smoke:** failed run `manual:san-jose-public-library-events:20260717-rss-smoke-1` stopped at
    the original 60-page cap with **zero observations/canonical attachments**. Successful
    `manual:san-jose-public-library-events:20260717-rss-smoke-2` then stored **3,813 candidates / 3,772 canonical
    attachments** in one durable attempt. The canonical window is 2026-07-17 09:30 through 2026-10-14 18:00
    Pacific. All 3,772 canonical events retain a source venue, normalized San José city, coordinates, end time,
    and description; every price is `unknown`. Current examples include Peer Support for MyConnectSV, Art
    Explorers Art House Summer Art Camp, Asistencia para Búsqueda de Trabajo, Arts & Crafts at Evergreen,
    Computación Básica para principiantes, and Make Your Own Mini Comic! No account, credential, sign-in, RSVP,
    purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** fixtures prove exact duplicate-query ordering, all-branch allowlisting,
    local-window clipping, physical/hybrid retention, safe GUID/link agreement, virtual/cancelled/past/wrong-
    location/unsafe exclusion, pacing, tampered query/cap refusal, and 160-page overflow failure. Registry
    integration asserts the exact seed/origin/mode/cap. `make install && make up && make migrate && make test &&
    make test-integration && make lint typecheck` passes (**159 unit, 19 integration; 107 source files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 29 COMPLETE: Contra Costa County Library official RSS calendar.**
  Migration `0037` adds the separately reviewed `contra-costa-county-library-events` profile under the closed
  `bibliocommons_rss` mode. It is pinned to Contra Costa County Library's anonymous BiblioCommons gateway endpoint
  and to one exact, ordered, repeated `locations` query containing the 25 current physical-branch facets; the
  virtual facet is deliberately absent. The adapter accepts only that HTTPS gateway host/path/query, then creates
  only matching GUID/link handoffs on `ccclib.bibliocommons.com/events/...`; it never follows a handoff, event
  detail, ticket, registration, or external URL. The source-bound profile also makes registry query reordering,
  branch widening, or page-cap changes fail before any request.
  - **Completeness / normalization:** the publisher's public events index reported 2,606 events for the same
    closed current physical-location set. At 25 RSS rows per page, the reviewed 105-page ceiling covers that
    broader view while the local Pacific 90-day request adds `startDate`/`endDate` only at request time and stops
    at its first short page. If every reviewed page is full, the durable refresh fails rather than publishing a
    partial source. It retains only future, non-cancelled physical or hybrid rows with a reviewed location ID,
    matching safe GUID/link identity, valid UTC start, and an occurrence inside the half-open local horizon.
    Virtual-only, wrong-location, cancelled, malformed, past, out-of-horizon, unsafe, and mismatched-identity
    rows are excluded. Source venue, city, coordinates, end time, and sanitized description are retained; absent
    structured price remains `unknown`. BiblioCommons permits RSS/XML automation, but its service-content terms
    include a personal/non-commercial clause; this source should receive owner/legal review before commercial
    redistribution.
  - **Live read-only smoke:** `manual:contra-costa-county-library-events:20260717-rss-smoke-1` stored **1,457
    candidates / 1,364 canonical attachments** in one durable attempt, terminating within its closed page cap.
    The canonical window is 2026-07-17 10:00 through 2026-10-14 18:30 Pacific. All 1,364 canonical events retain
    a publisher venue, normalized city, coordinates, end time, and description; every price is `unknown`. The
    normalized records span Antioch, Bay Point, Brentwood, Clayton, Concord, Crockett, Danville, El Cerrito, El
    Sobrante, Hercules, Kensington, Lafayette, Martinez, Moraga, Oakley, Orinda, Pittsburg, Pleasant Hill, Rodeo,
    San Pablo, San Ramon, and Walnut Creek. No account, credential, sign-in, RSVP, purchase, event-detail fetch,
    calendar write, or source mutation occurred.
  - **Coverage / verification:** fixtures prove the exact duplicate-query ordering, all-branch allowlisting,
    local-window clipping, physical/hybrid retention, safe GUID/link agreement, virtual/cancelled/past/wrong-
    location/unsafe exclusion, pacing, short-page termination, tampered query/cap refusal, and 105-page overflow
    failure. Registry integration asserts the exact seed/origin/mode/cap. `make install && make up && make migrate
    && make test && make test-integration && make lint typecheck` passes (**163 unit, 19 integration; 107 source
    files**).
  - **NEXT:** continue only with separately reviewed official Bay Area publisher feeds that satisfy the current
    source-truth physical-location boundary; preserve the explicit, manual, read-only, handoff-only boundary and
    exclude Meetup, credentials, sign-in, RSVP, payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 30 COMPLETE: University of San Francisco Main Campus public calendar.**
  Migration `0038` adds the separately reviewed `usfca-main-campus-events` profile under a closed `usfca_html`
  mode. It is pinned to exactly
  `https://www.usfca.edu/life-at-usf/events?field_campus%5B179%5D=179`, with one approved origin and a one-page
  cap. The adapter requires exactly one publisher `cc--events-listing` results container and parses only cards
  inside it: visually similar featured cards elsewhere on the page are deliberately out of scope. It constructs
  only safe relative `www.usfca.edu/event/<slug>/<positive-id>` handoffs and never follows a handoff, event
  detail, ticket, registration, external application, or other URL.
  - **Completeness / normalization:** the reviewed filtered list has no pager. A pager or `page` query inside its
    results container, an altered seed/query, a redirect, a missing/ambiguous container, or a response larger than
    the reviewed limit fails the whole refresh rather than silently accepting a broadened or truncated surface.
    The adapter accepts only a future single-day `Month D, YYYY h:mmAM - h:mmPM` source range and a nonempty
    physical source location. Virtual/online, cancelled/postponed, past, malformed, missing-location, unsafe, and
    duplicate rows are excluded. It retains the source venue and end time, but the list provides no structured
    locality, coordinates, description, or offer: city and geo remain absent, description remains empty, and
    price remains `unknown` rather than being inferred. The source's literal display markup includes a few
    duplicated venue words; those remain publisher text rather than being heuristically rewritten.
  - **Live read-only smoke:** `manual:usfca-main-campus-events:20260717-html-smoke-1` made **1** paced anonymous
    public list GET and stored **8 candidates / 8 canonical attachments** in one durable attempt. The canonical
    window is 2026-08-19 08:00 through 2026-11-19 18:00 Pacific. All eight retain a source venue, end time, and
    unknown price; none invents city, geo, or description. Current examples include Res Hall Check In, Orientation
    Welcome Center & Help Desk, New Student & Family Orientation Welcome, Student Employment Part-Time Job Fair,
    Opening Celebration for Market/Value, First 50 Days, and the Leo T. McCarthy Award and 25th Anniversary
    Celebration. No account, credential, sign-in, RSVP, purchase, event-detail fetch, calendar write, or source
    mutation occurred.
  - **Coverage / verification:** fixtures prove exact seeded-query closure, list-container isolation from an
    otherwise valid-looking featured card, source-built handoffs, local time/end normalization, physical-only
    retention, virtual/cancelled/past/malformed/external/unsafe exclusion, redirect refusal, no-detail behavior,
    one-page contract failure, and per-host pacing. Registry integration asserts the exact seed/origin/mode/cap.
    An independent source-safety review verified the post-fix boundary. `make install && make up && make migrate
    && make test && make test-integration && make lint typecheck` passes (**166 unit, 19 integration; 109 source
    files**).
  - **NEXT:** the separately reviewed Cal Performances public collection is a candidate for the next increment;
    retain the same manual, read-only, handoff-only boundary and exclude Meetup, credentials, sign-in, RSVP,
    payment, and calendar mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 31 COMPLETE: Cal Performances official public collection.**
  Migration `0039` adds the separately reviewed `calperformances-events` profile under a dedicated closed
  `calperformances_json` mode. It is pinned to exactly
  `https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=1`, one approved origin, a four-page
  ceiling, and a 1.5-second per-host floor. The adapter constructs each collection page itself with only the
  reviewed `per_page=100&page=N` query; it never follows publisher pagination, individual performance, ticket,
  registration, or external URLs.
  - **Completeness / normalization:** the public collection declares `X-WP-Total` and `X-WP-TotalPages`. The
    refresh requires those headers on every page to stay invariant, requires their exact 100-row page arithmetic,
    validates every positive WordPress ID globally unique, and verifies the accumulated raw count equals the
    declared total. It fails closed on an absent/inconsistent header, a fifth page, short/intermediate page,
    duplicate ID, redirect, endpoint drift, oversized body, or malformed JSON. The current publisher declaration
    is 381 records across four pages. Each retained occurrence must have two identical responsive `addeventatc`
    blocks with source title, local start/end, exact `America/Los_Angeles`, and a nonempty physical location. The
    WordPress title must agree with the structured title except for the two source-observed curly-versus-ASCII
    apostrophe renderings; no fuzzy title reconciliation occurs. It parses only those structured blocks, not the
    collection's full presentation HTML or prose; description, city, and geo therefore remain absent. A source
    `Tickets start at $<positive>` phrase becomes `paid`; all other pricing stays `unknown`, never inferred free.
    The API returns `X-Robots-Tag: noindex`, a search-index directive rather than a crawl denial; `robots.txt`
    permits `/wp-json`. The public policy review found no explicit automation/reuse term, but owner/legal review
    remains appropriate before commercial redistribution.
  - **Live read-only smoke:** `manual:calperformances-events:20260717-json-smoke-1` made **4** paced anonymous
    public collection GETs and stored **4 candidates / 4 canonical attachments** in one durable attempt. The
    canonical window is 2026-09-25 19:30 through 2026-10-11 15:00 Pacific. All four retain a source venue and
    end time and are explicitly `paid`; none invents city, geo, or description. Current entries are The Australian
    Ballet: Oscar at Zellerbach Hall, Lester Lynch / Kevin Korth at Hertz Hall, Tom Borrow at Hertz Hall, and
    Takács Quartet with Jeremy Denk at Zellerbach Hall. Publisher-supplied title text, including its literal
    `2627` suffix on a few records, is retained rather than heuristically rewritten. No account, credential,
    sign-in, RSVP, purchase, event-detail fetch, calendar write, or source mutation occurred.
  - **Coverage / verification:** fixtures prove exact source timestamp punctuation, responsive-block/title
    agreement, apostrophe-only title equivalence, safe same-origin handoffs, local time/end and DST ambiguity
    handling, physical/future retention, explicit paid versus unknown pricing, virtual/cancelled/past/out-of-
    window/malformed/unsafe exclusion, no-detail behavior, pacing, redirect/size/tamper refusal, pagination cap,
    total drift, raw-count inconsistency, and duplicate-ID failure. Registry integration asserts the exact
    seed/origin/mode/cap, and an independent source-safety review found no blocker. `make install && make up &&
    make migrate && make test && make test-integration && make lint typecheck` passes (**172 unit, 19 integration;
    111 source files**).
  - **NEXT:** Berkeley Repertory Theatre's public show list is separately researched but needs its stronger
    five-second crawl floor and an owner/legal redistribution review carried forward; retain the same manual,
    read-only, handoff-only boundary and exclude Meetup, credentials, sign-in, RSVP, payment, and calendar
    mutation.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 32 COMPLETE: Berkeley Repertory Theatre public show list.**
  Migration `0040` adds the separately reviewed `berkeley-rep-shows` profile under a dedicated closed
  `berkeley_rep_html` mode. It is pinned to exactly `https://www.berkeleyrep.org/shows`, with the official
  `tickets.berkeleyrep.org` origin recorded only for validated human handoffs. The worker fetches only the
  query-free show list; it never opens a show detail, ticket, registration, or other URL. The profile locks the
  publisher's `robots.txt` `Crawl-delay: 5` as a five-second per-host floor.
  - **Completeness / normalization:** the current server-rendered list has one `ul.listItems`, seven source cards,
    310 raw occurrence rows, and no pager. The adapter requires that exact container and direct
    `li.eventCard[data-entry-id]` shape, fails if a pager/page link appears or the known nine-card capacity is
    reached, and fails before normalization if 400 raw occurrence rows are reached. Within a card it requires the
    source title/date range, an ID-bound direct occurrence row, source weekday/month/day and time, named physical
    venue, and an exact official numeric ticket handoff. A no-year occurrence date is resolved only when it has one
    weekday-valid date inside the publisher's full parent range; ambiguous/malformed dates are excluded. The list
    supplies no event end, coordinates, locality, or price, so those remain absent/`unknown`; source tagline is
    retained as description. Invitation-only, virtual/online, cancelled/postponed, malformed, unsafe, past, and
    out-of-horizon rows are excluded. The site exposes a privacy policy but no separate public reuse grant; this
    handoff source needs owner/legal review before commercial redistribution.
  - **Live read-only smoke:** `manual:berkeley-rep-shows:20260717-html-smoke-1` made **1** paced anonymous public
    list GET and stored **42 candidates / 42 canonical attachments** in one durable attempt. The canonical window
    is 2026-09-04 20:00 through 2026-10-11 14:00 Pacific. All 42 retain a source venue and tagline description;
    none invents end time, city, geo, or price. The current in-window occurrences are The Cook at Peet’s Theatre.
    No account, credential, sign-in, RSVP, purchase, event-detail fetch, ticket fetch, calendar write, or source
    mutation occurred.
  - **Coverage / verification:** fixtures prove exact show-list containment (including featured-card exclusion),
    direct official ticket handoffs, no-detail behavior, source tagline retention, year-boundary date resolution,
    physical/future retention, invitation-only/virtual/cancelled/past/out-of-window/malformed/unsafe exclusion,
    five-second pacing, query/origin/cap tampering, redirects, response-size failure, pager/card/occurrence caps,
    and structural-contract failure. Registry integration asserts exact seed/origins/mode/cap. An independent
    technical review found no source-safety blocker. `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` passes (**178 unit, 19 integration; 113 source files**).
  - **HELD:** San Francisco Conservatory of Music has an otherwise workable public monthly performance list, but
    its terms prohibit public/commercial reuse of site content without written permission. Do not implement it
    unless the owner confirms an authorized private/noncommercial scope or obtains permission.

- **2026-07-17 — BAY AREA CATALOG INCREMENT 33 COMPLETE: Yerba Buena Center for the Arts public calendar.**
  Migration `0041` adds the separately reviewed `ybca-calendar` profile under a dedicated closed `ybca_html`
  mode. It is pinned to exactly `https://ybca.org/calendar/`, its sole approved origin, one page, and the
  publisher's `robots.txt` ten-second crawl floor. The worker reads only that SSR calendar and creates safe
  same-origin `/event/<lowercase-hyphen-slug>/` handoffs; it never opens those pages or the external Veevart ticket
  links rendered in the cards.
  - **Completeness / normalization:** the current list has one `div.events` calendar container, 31 scoped cards,
    no pager, and a 94,975-byte response. A separate featured card is outside the container and excluded. The
    adapter fails on a missing/ambiguous container, pager/page link, a 40-card list, redirect, endpoint drift, or a
    response above the 250 KB reviewed bound. Within a card it requires exactly one title/handoff, date, and the
    date-adjacent venue selector `.tickets > p.date + p > strong`. That narrow selector is intentional: a live-card
    review found later bold `Tickets:`/price labels that must not be mistaken for a venue; the regression fixture
    locks this shape down. It accepts only an exact future single-day local `h[:mm] PM/AM` form or YBCA's verified
    same-final-meridiem range form such as `2–4 PM`; multi-day, ongoing, cross-noon-looking, malformed, and DST-
    ambiguous strings are excluded. A named venue must end in publisher-provided `, YBCA`, yielding source-grounded
    San Francisco city; virtual/online, cancelled/postponed, unsafe, past, and out-of-window cards are excluded.
    Source end time is retained only for parsed ranges. Geo and description are absent and price is `unknown` rather
    than inferred. The published Terms review was privacy-focused and found no automation/reuse prohibition, though
    ordinary owner/legal review remains appropriate before commercial redistribution.
  - **Live read-only smoke:** `manual:ybca-calendar:20260717-html-smoke-1` made **1** paced anonymous public list
    GET and stored **21 candidates / 21 canonical attachments** in one durable attempt. The canonical window is
    2026-07-17 18:30 through 2026-10-10 14:00 Pacific. All 21 retain a source venue and San Francisco city;
    **10** retain a source end time, every price is `unknown`, and none invents geo or description. Current examples
    include The World of Black Film: “Compensation” + Book Signing, Free Art Workshop: Flower Applique Embroidery,
    Tour of Conjuring Power, Phillip B. Williams Poetry Reading, Opening Night Celebration for GaHee Park, and
    Creative Power Moves. No account, credential, sign-in, RSVP, purchase, event-detail fetch, ticket fetch,
    calendar write, or source mutation occurred.
  - **Coverage / verification:** fixtures prove calendar containment/featured exclusion, exact safe handoff,
    date-adjacent venue extraction despite later ticket-label strong text, single-time/range grammar, horizon/DST
    handling, physical/future retention, virtual/cancelled/multi-day/unsafe exclusion, no-detail/no-ticket
    behavior, ten-second pacing, query/origin/cap tampering, redirects, body/card/pager failures, and duplicate
    conflict. Registry integration asserts exact seed/origin/mode/cap; an independent review caught and verified
    the live venue-selector correction before migration. `make install && make up && make migrate && make test &&
    make test-integration && make lint typecheck` passes (**184 unit, 19 integration; 115 source files**).
  - **HELD:** City of Hayward's otherwise technical calendar and SFCM's performance calendar both have terms that
    require permission for public/commercial republication. Do not implement either without owner/legal approval.
  - **NEXT:** continue only with separately reviewed publishers whose public list, robots policy, and content-rights
    posture fit the existing manual, read-only, handoff-only boundary; exclude Meetup, credentials, sign-in, RSVP,
    payment, and calendar mutation.

- **2026-07-16 — Codex built 5 build increments; reviewed + validated.** Codex (owner using Codex CLI)
  implemented: granular paced Temporal saga (once-minted keys, read-before-mutate, out-of-band confirm
  verify, forward-recovery, parent/child directive protocol); durable outbox relay + notifier worker
  (notification_ledger claim/lease); personalized ranking (PersonalizedRanker + real Cohere rerank adapter,
  deterministic offline default); fixture-backed Meetup/Luma SourcePort adapters (new narrow ports
  meetup/browser); swappable Google Calendar adapter (new google_calendar ports + calendar_bindings table).
  Migrations 0004 (outbox_relay + notification_ledger) + 0005 (calendar_bindings, FORCE RLS). 77 src
  modules; **48 unit + 13 integration tests green, mypy strict + ruff clean** (I re-ran all).
  - **Adversarial review (6-agent workflow `wf_c0e40b7d-81d`): code is high-quality + faithful; NO
    correctness bug on the happy path, NO invariant regression** (RLS/ec_app/NULLIF intact; calendar_bindings
    RLS-forced + tested; notification_ledger no-RLS is defensible = cross-tenant relay table like outbox;
    composition still single root; offline/no-creds genuinely proven; deterministic ids intact). Saga
    milestone = SOLID (exactly-once genuinely proven via 3 crash/replay tests + forward-recovery + directive
    tests). Others = minor-issues, all TEST-DEPTH not correctness.
  - **Findings to fix (in the Codex follow-up prompt):** (1) REAL BUG — Google `_raise_for_error` maps every
    401/403 to re-consent without inspecting the error `reason`; a 403 rate/quota would wrongly trigger
    re-consent. (2) REAL edge bug — outbox `attempt_count` increments on BUSY/transient reschedules, so
    contention can exhaust the 5-attempt terminal-fail budget. (3) OWNER-ASK GAP — the scroll/dwell
    implicit-feedback loop is scaffolded (`implicit_affinities` plumbed+weighted) but nothing writes signals
    from behavior; the "scroll adjusts to preferences" feature is not built. (4) Cohere + Google real
    adapters are unreachable (no settings key / no mock_cloud=false branch) = dead until wired. (5) TEST-DEPTH
    gaps (correct-on-inspection, untested -> would regress silently): the durable ledger DELIVERED/BUSY dedup
    (the outbox's whole point converges via MockNotifier in-mem dedup, not the ledger); the 2nd data-plane
    policy guard blocking the mutation; fail-closed membership (UNKNOWN/exception drops AUTONOMOUS_SLA);
    signal-without-confirmed-read stays AWAITING; Meetup read-before-mutate no-op (AC-36) + FR-3.2 modality
    refusal + FR-5.10 paywall; Google happy-path insert (200 no-patch) + IANA-reject; Cohere defensive parse.
  - Follow-up prompt for Codex saved conceptually in this session; foundation + increments NOT committed.

- **2026-07-16 — FOLLOW-UP HARDENING COMPLETE: Google classification and outbox retry accounting.**
  Google Calendar now requests re-consent only for explicit credential/scope failures (`401`,
  `invalid_grant`, and documented authorization reasons); quota, rate-limit, and other `403` responses remain
  ordinary adapter errors rather than falsely prompting consent. Offline fixtures cover both paths.
  - **Outbox:** leasing/reclaiming a row no longer increments `attempt_count`. Only a known
    `NotificationPort.send()` exception atomically consumes retry budget; notification-ledger contention and a
    lost ledger lease defer for two seconds without budget consumption. Unit and PostgreSQL integration
    coverage exercise repeated BUSY deferrals, a lost ledger lease, terminal fifth failure, and persisted
    zero-attempt success/contention rows. No migration was required; historical pending counts are preserved.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**57 unit, 14 integration; 77 source files**). Meetup remains deliberately
    disabled; no external credential, sign-in, RSVP, calendar write, or source mutation was performed.

- **2026-07-17 — SYSTEM-TEST HARDENING COMPLETE: workflow, lifecycle, delivery, calendar, ranking, and RLS.**
  Migration `0042` restores ADR-007's missing database transition choke point: `fn_transition` now reserves a
  transition id, validates the legal state edge and caller tenant context, and commits lifecycle state, ledger,
  outbox, and (for handoff routing) the task in one transaction. It runs with a fixed search path and explicit
  `NULLIF(current_setting('app.tenant_id', true), '')` tenant check; `ec_app` no longer has a direct lifecycle
  `UPDATE` or transition-ledger `INSERT` path around that guard. This fixes concurrent activity retries that could
  previously raise after a competing transition had committed rather than converging as an idempotent no-op.
  - **Workflow and delivery regressions:** Temporal/Postgres tests now cover a kill switch flipped after the
    early policy activity but before wire mutation (zero RSVP effect), an opaque confirmation signal whose fresh
    source read remains pending (no calendar/outbox advance), and duplicate `{tenant}:{event}` child starts
    (one execution/effect). The relay now proves a row with a persisted `notification_ledger=delivered` is
    acknowledged on redelivery with no second user-visible send.
  - **Safety, tenant, and provider contracts:** registration re-checks `freeBusy` immediately before source I/O;
    both a new hard overlap and a calendar-check outage fail safe to handoff with zero RSVP effect. Request reads
    now require the owning tenant context and have an adversarial RLS test. Offline tests cover Google’s direct
    successful insert without a patch and prohibit writing to `primary`; Cohere malformed, duplicate, incomplete,
    out-of-range, and non-finite rerank responses fail closed.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**193 unit, 26 integration; 115 source files**). The increment used only local Docker,
    fixtures, and mock adapters; it made no external source, credential, RSVP, payment, or calendar mutation.

- **2026-07-17 — PRE-INTEGRATION EXECUTION TRACK (ACTIVE; owner-directed).** Before activating any
  credentialed provider or live registration/calendar path, finish the locally runnable reliability and product
  contracts below in this order. Each increment must add focused unit/integration coverage, pass the full green
  bar, and receive a completion note here before the next begins; after a verified increment, proceed directly to
  the next unless an owner decision or external credential is genuinely required. No commit or live external
  mutation is authorized by this track.
  1. **P0 — Durable idempotent request intake (complete).** Replace the API's UUID-per-POST, best-effort Temporal
     start with ADR-003's transactional **start-outbox**: deduplicate on `(tenant, normalized request text,
     time bucket)`, persist the parsed `EventRequest` and durable start instruction atomically, relay/retry starts
     with Temporal reject-duplicate semantics, and prove retry/crash recovery starts exactly one parent workflow
     (FR-6.8, FR-8.1, AC-48, NFR-8). This requires only local Postgres and Temporal.
  2. **P1 — ADR-005 Pacer contracts (complete; credential-free).** This is deliberately split so the completed
     generic safety core is not overstated as full provider-quota compliance:
     - **P1a — shared-state/outage proof (complete).** Run the real `RedisPacer` against local Redis across
       concurrent workers; cover atomic bucket sharing, script fallback, source-reset/retry-after behavior where
       applicable, and Redis-loss throttle-first recovery without a burst. Source activities return a projection;
       Temporal owns the durable re-acquire timer (ADR-003/005, FR-10.4, AC-73).
     - **P1b — typed source profile boundary (complete).** Carry source, opaque credential/calendar scope, operation,
       and cost through the Pacer; install the ratified default profile shapes as offline fixtures. This does not
       activate a provider or assert unmeasured RSVP-point costs.
     - **P1c — Ticketmaster daily-budget ledger (complete).** The guarded PostgreSQL authorization ledger enforces
       the configured shared app scope's 5,000/day hard ceiling and 4,500 crawl / 500 reserve partition at
       source-initiation authorization time, before any future Ticketmaster client; Redis remains only the
       disposable rps politeness layer (ADR-002/005).
     - **P1d — Meetup app-scope fairness/degrade (complete).** Redis DRR/FIFO lanes, stable workflow queue
       identities, strict five-minute projected wait → `DEGRADE` → `SATURATION` handoff, and the G2-gated
       configuration flip are covered offline. G2 still gates enabling that mode or declaring an SLA.
     - **P1e — browser admission cap (complete).** A distinct held-lease boundary enforces the 135-slot cap with
       a five-minute Redis-loss recovery fence, fenced cleanup, and browser-only saturation handoff before a real
       browser RSVP lane is enabled (AC-45, NFR-4b, ADR-005/006).
  3. **P2 — Notifier wake-up and operational proof (complete).** Upgrade the existing outbox poll worker to PostgreSQL
     `LISTEN/NOTIFY` with its two-second poll fallback, then cover restart/redelivery, readiness, and local
     latency/queue observability (ADR-009, FR-6.6, FR-8.9). Real SES delivery events and sending-domain warm-up
     remain provider activation work.
  4. **P3 — Offline lifecycle and calendar contract completion (complete).** Complete mock/fixture-backed cancellation,
     reconcile, and un-RSVP behavior, and the Google adapter's canonical/fuzzy duplicate plus incremental-sync
     contracts (FR-8.7/8.8, FR-9.3/9.4). Do not implement D1–D8 behavior: in particular, D6 out-of-band
     verification remains deferred pending requirements v0.3 sign-off.
  5. **P4 — Engine payload and trust-boundary contracts (P4a/P4b/P4c/P4d complete).** The mockable claim-check/object-
     store boundary now protects oversized Temporal payloads, raw intake text stays in the RLS-protected request row,
     API tenant scope comes only from an injected authentication context—not caller JSON, and RelayInbox secret
     contracts carry opaque references only. The bounded external-artifact purge contract now safely exercises known
     calendar/vault/claim cleanup without claiming full account erasure (FR-8.5, AC-58, FR-1.1/1.3, FR-2.6,
     FR-10.5, ADR-003/007/010/011). Full erasure still needs retention, database, relay, and workflow-fencing work;
     real KMS, OIDC, relay-domain, and object-store activation remain external integration work.
  6. **P5 — Repeatable end-to-end quality and bounded repetition harness (P5a/P5b complete).** The checked-in
    synthetic corpus drives a deterministic local system journey (`intake → catalog → rank → policy/conflict → mock
    registration → calendar → notification`) through the real Temporal spine, with duplicate/restart fault
    overlays. `make quality` is the one-cycle semantic matrix; `make quality-load` repeats it in bounded serial
    cycles over isolated tenant namespaces. The serial posture is deliberate because activity composition is
    process-global; it is not a capacity or latency benchmark. This is G1-style semantic evidence only, not a live
    source, provider-SLA, or G1-closure claim; a production-realistic corpus still requires owner-provided requests.
  7. **P6 — Offline audit/completeness foundation (P6a complete; P14c hardens new registration facts).** The registration saga now records immutable,
    tenant-scoped policy precheck, authoritative pre-mutate, and normalized source-RSVP outcome facts using the
    child workflow's once-minted source key. The app role can inspect only its own PII-minimized rows and can write
    only through a guarded append function. P14c now binds each new allowed registration fact to an exact opaque,
    tenant/source/modality-scoped consent reference while retaining exact replay of historical nullable P6a rows.
    This remains intentionally narrower than trusted consent capture, credential-access audit, legal-retention/
    erasure, or a full compliance claim.
  8. **P7 — Durable safety/recovery foundations (P7a/P7b/P7c complete).** The action-moving policy PDP reads fresh,
    owner-controlled PostgreSQL source/quarantine and global/per-tenant kill-switch state, denying on control-plane
    failure; the nightly scanner separately observes lifecycle, watch, handoff, and Temporal divergence without
    becoming a writer; and a typed ban/403 can now trip a narrowly-authorized, one-way source circuit breaker.
    Registration, withdrawal, discovery, and catalog dispatches re-read the relevant policy before their source
    boundary, so a persisted quarantine reaches direct handoff rather than a retry loop or another provider call.
    This closes the local-only policy, ban-actuation, discovery-gating, and visibility gaps without inventing
    RSVP-period accounting, the deferred D1 concurrent-open cap, B12's unratified freeze-grace behavior, or
    production backup/PITR. Re-rank the remaining offline-safe gaps before beginning the next slice.
  9. **P8 — Durable ranking inputs (P8a complete).** The existing cross-encoder and per-user re-score now read
    tenant-isolated, revisioned explicit/implicit affinity maps from PostgreSQL under FORCE RLS. A whole-profile
    update converges exactly across an at-least-once delivery: a higher revision applies, the same revision with
    exactly the same normalized profile replays, a conflicting same revision fails, and a lower revision is stale.
    This is storage for already-aggregated inputs only; it deliberately does not choose onboarding, feedback,
    merge/decay, model-training, or D1–D8 product semantics (FR-1.2--FR-1.4, FR-2.1, FR-4.3/FR-4.4, ADR-001).
  10. **P9 — Tenant identity isolation (P9a complete).** The former onboarding exception, `tenants`, is now a
    FORCE-RLS identity/contact read model. Runtime access is SELECT-only under an established tenant GUC; a
    fixed, idempotent provisioning capability is the sole application write path and hides binding conflicts.
    This closes an otherwise global readable/mutable store of OIDC subject, notification email, and RelayInbox
    values without claiming real OIDC verification or relay-domain provisioning (FR-1.2--FR-1.5,
    AC-1/AC-3/AC-4, NFR-7, ADR-001/011).
  11. **P10 — Catalog cadence dispatch (P10a complete).** The previously unused per-source refresh interval now
    produces a bounded, deterministic one-shot batch over reviewed source slots. A latest successful manual or
    scheduled refresh advances the same durable cursor; failed/retried work reuses one source/UTC-slot run key
    through the existing lease, Pacer, and fresh policy fence. The explicit worker is schedule-compatible but does
    not provision recurrence or enable public crawling, so it is plumbing for—not a claim of—ADR-001's eventual
    Temporal micro-cycle/Ticketmaster plan or NFR-1 freshness (FR-3.3/FR-3.9, NFR-1/NFR-8, ADR-001/004).
  12. **P11 — Organizer-change watch-poll control plane (P11a complete).** A public, PII-free cursor now records
    distinct active watch timing, exact leases, success/failure class, next due time, and initial-registration age.
    An explicitly supplied fixture cadence plan drives a bounded sequential poll → durable ledger record → cursor
    acknowledgement path; it never hard-codes the still-unratified B24/B25 source intervals. Preflight and
    postflight health expose pending, untracked, stale, and failure-alarmed coverage without making a source call.
    This is durable offline scheduling/visibility plumbing—not a deployed detector, worker, source adapter, or
    NFR-17/AC-60 claim (FR-8.7a, NFR-17, ADR-008).
  13. **P12 — ADR-008 global control-plane capability boundary (P12a/P12b complete).** The non-superuser app role can
    no longer directly read or mutate opaque tenant/workflow fanout, repair, organizer-ledger, lifecycle-projection,
    watch-subscription, or public watch-poll cursor rows—or advance the guarded queue identity sequences. Fixed-shape,
    fixed-search-path database
    capabilities now own public-change recording/fanout, aggregate active-watch listing, exact lease claim/ack/retry,
    closed-workflow repair enqueue, repair-ledger checks, and public poll-cursor claim/result/health reads. This closes
    residual global queue/cursor fabrication, suppression, redirection, raw tenant/workflow existence, lease-token
    disclosure, and sequence-advance paths without enabling a detector or touching a provider (NFR-7/NFR-8,
    ADR-007/008).
  14. **P13 — Missing-tenant-context audit boundary (P13a complete).** Database work now declares whether it is
    tenant access or intentional tenant-neutral catalog/control-plane work. An explicit missing tenant identity
    commits one PII-free private policy-violation event before yielding an empty-GUC, FORCE-RLS transaction; it
    therefore returns zero tenant rows and retains evidence even when a rejected write rolls back. This covers
    application-managed tenant-session attempts, not arbitrary raw SQL outside that boundary (FR-1.3/FR-1.4,
    AC-3, NFR-7).
  15. **P14 — Pre-integration trust boundaries (P14a/P14b/P14c complete).** The reviewed public-source registry is now
    migration-owner controlled rather than a mutable application control plane, and durable catalog refresh runs
    advance only through bounded, exact-lease database capabilities using the database clock. The refresh service
    re-reads the registry after Pacer acquisition and before its one possible fetch, so an owner disable, expiry,
    or adapter revision made during a wait prevents egress. A separate fixture-only RelayInbox seam now parses
    bounded local MIME deterministically and hands off only an opaque, consumed-once reference; it neither makes a
    provider/domain call nor signals a workflow. P14c adds an immutable, owner-seeded, FORCE-RLS registration-
    consent registry with fixed tenant-derived resolve/validate capabilities only: `ec_app` cannot read or mutate
    the table. The registration saga needs that evidence before every source read and revalidates the same opaque
    reference immediately after its mutation Pacer lease and before the adapter call; the guarded audit append
    requires a matching current reference for all new allowed facts while historical nullable replay converges
    unchanged. This is deliberately limited to Meetup/API and Luma/browser registration, not consent capture,
    account linking, revocation/replacement, credentials, calendar, withdrawal, or any provider activation. Next:
    only an owner-ratified trusted capture/disconnect/re-consent contract can broaden that boundary; G3, real
    relay-domain/provider work, and source activation remain held (FR-2.6, FR-2.9, FR-5.7/5.8, FR-7.3, FR-10.3,
    NFR-8/10, ADR-001/003/004/011).
  16. **P15 — Catalog HTTP egress boundary (P15a/P15b/P15c/P15d/P15e complete; credential-free).** P15a proves the incremental,
    durable execution path only for Mountain View Public Library's closed, one-document LibCal feed: its exact
    physical `GET` crosses a typed shared Pacer admission, a denied admission retries the same durable source/run
    identity through a Temporal timer, and typed 429/backoff and 403/quarantine handling are preserved. P15b/P15c/P15d/P15e
    extend that spine only to San Jose, Sunnyvale, Alameda, and Oakland's separately reviewed closed Legistar Events
    profiles: each fixed OData page has its own shared origin-level Pacer admission, durable Temporal activity, source revision,
    normalized no-raw staging, and atomic catalog/observation/run promotion. Database contract guards lock and
    re-read the current profile at both stage and final promotion, so a winning owner edit cannot publish an old page.
    The generic direct service and both operational entrypoints refuse/fence all four paged profiles rather than
    letting them fall back to process-local pacing. Other redirecting or paginated modes deliberately remain
    unconverted until they receive their own reviewed cursor contract. Owner
    registry/policy rechecks fence every resumed request. This is safety plumbing only—not
    public-crawl enablement, provider activation, throughput/SLA claim, or a substitute for source-specific reuse
    approval (FR-3.9, FR-10.3/10.4, NFR-8, ADR-003/004/005).
  - **Explicitly held:** requirements v0.3 D1–D8, G2 Meetup Pro OAuth/quota validation, G3 relay-domain
    validation, Google OAuth/production calendar binding, real SES/domain activation, and any provider API key or
    account provisioning. These are not silently substituted or worked around.

- **2026-07-17 — P0 COMPLETE: durable, idempotent EventRequest intake/start-outbox.** Migration `0043` adds
  `request_start_outbox`, an opaque global lease/retry queue keyed by the deterministic request ID and its
  normalized-text/UTC-hour dedup key. It deliberately contains no raw request text: the relay re-opens the RLS-
  protected `event_requests` row under the recorded tenant context before it ranks candidates; the start relay passes
  only the opaque deterministic parent identity to Temporal. The API now persists the request and start instruction
  atomically, makes one leased low-latency attempt, and returns a durable pending start if Temporal is unavailable;
  `workers/request_starter.py` continually replays pending rows, and `make request-starter` runs it locally.
  - **Replay semantics:** `intake_request_id()` mints the same UUID for normalized duplicate submissions in a UTC
    hour. `TemporalRequestWorkflowStarter` treats only Temporal `REJECT_DUPLICATE` as an already-successful start;
    every other engine error is retained with capped exponential retry delay and no terminal-drop path. A lost
    database acknowledgement therefore replays safely against `req:{tenant}:{request}` rather than creating a
    second parent.
  - **Coverage:** unit tests prove normalized HTTP/provider retries create one request/start instruction and a
    simulated post-effect Temporal acknowledgement loss has one parent effect. PostgreSQL tests prove atomic
    rollback on an impossible dedup collision, no raw text in the cross-tenant queue, durable retry/state update,
    and real Temporal reject-duplicate convergence. The API test proves two normalized POSTs return one request /
    workflow ID and invoke one starter effect.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**195 unit, 30 integration; 118 source files**). No external credential, source call, RSVP,
    calendar write, payment, or commit was made. **NEXT: P1 Redis Pacer shared-state and outage proof.**

- **2026-07-17 — P1a COMPLETE: non-blocking Redis Pacer safety core.** `Pacer.acquire()` now returns a typed
  one-shot lease rather than parking an activity in an async context manager. A non-granted lease crosses the
  activity boundary as data; the registration child uses a Temporal durable timer, then starts the required fresh
  membership/read-before-mutate/confirmation-read sequence again with its once-minted source idempotency key.
  The catalog refresh path releases its durable run claim and returns `DEFERRED` rather than sleeping when paced.
  - **Redis safety:** the Lua hot path takes Redis `TIME`, initializes a missing bucket at zero tokens, and returns
    a wait. Its WATCH/MULTI script fallback has the same cold-start invariant; Redis transport loss returns a
    throttle-first projection. `observe_backoff()` records the longest `retry-after`/reset timestamp block and
    clears retained capacity before re-entry (ADR-005, AC-73).
  - **Coverage:** local Redis integration tests prove eight independent clients consume only a shared burst, atomic
    costs and credential isolation, namespaced bucket eviction with zero immediate grants, cross-worker
    `retry-after`, and a real-pipeline no-Lua fallback. A Temporal/Postgres regression freezes time at a paced
    pre-mutate lease, proves zero RSVP before the timer, then proves one RSVP/effect/key after re-acquisition.
    Unit coverage proves an unavailable Redis client never fails open.
  - **Verification:** `make lint typecheck test` passes (**198 unit**); `make test-integration` passes (**35
    integration**) against local Docker. No provider credential, external source request, RSVP, calendar write,
    payment, or commit was made. **NEXT: P1b typed source-profile boundary.**

- **2026-07-17 — P1b COMPLETE: typed Pacer input and normalized source-throttle boundary.** Every Pacer call now
  carries a `PacerRequest` with its source, opaque credential/calendar quota scope, operation, cost, optional tenant,
  and future fair-queue item ID; plaintext tokens never enter the request or Redis key. Composition installs the
  accepted offline profile shapes: Meetup 500 points/60 seconds (cost remains unmeasured until G2), Luma 100/5min,
  and Ticketmaster's conservative 2-rps crawl working rate under its external 5-rps maximum. P1c still owns the
  separate 5,000/day durable cap.
  - **429/reset propagation:** `SourceRateLimitedError` is a port-level signal carrying `retry-after` or an aware
    reset timestamp. Membership, read-before-mutate, mutation, confirmation verification, and catalog fetch paths
    record that signal through `Pacer.observe_backoff`, then return a workflow-owned wait; generic HTTP failures do
    not masquerade as quota evidence. The pre-mutation ADR-004 guard now runs before a token is spent, leaving the
    successful Pacer acquisition immediately adjacent to the wire mutation.
  - **Safety hardening:** `auto` Pacer selection preserves shared Redis in every non-mock deployment; forcing
    process-local pacing there is rejected. Non-finite rate/retry inputs are rejected before Lua/Temporal can see
    them. Redis fixtures now exercise both `retry-after` and `resetAt`, and concurrent WATCH/MULTI fallback clients.
  - **Verification:** `make lint typecheck test` passes (**205 unit**); `make test-integration` passes (**37
    integration**) against local Docker. No provider credential, external source request, RSVP, calendar write,
    payment, or commit was made. **NEXT: P1c Ticketmaster daily-budget ledger.**

- **2026-07-17 — P1c COMPLETE: guarded Ticketmaster daily-budget authorization ledger.** Migration `0044` creates
  an owner-controlled singleton `provider_budget_scope`, tenant-neutral `provider_budget_daily`, and immutable-per-
  attempt `provider_budget_ledger` control-plane tables, plus fixed-search-path `SECURITY DEFINER` functions. The
  only app-role mutation path atomically checks the configured shared app scope's ratified **5,000/day** ceiling and
  **4,500 crawl / 500 reserve** partition using PostgreSQL's own UTC clock; `ec_app` has `SELECT` only on the daily
  and audit tables, no registry DML, and cannot insert, update, or delete around the guard.
  - **One physical request per permit:** A future Ticketmaster adapter must first obtain the shared 2-rps Pacer
    lease, then call `TicketmasterDispatchGate` immediately before one wire request. A `granted` decision is valid
    only on its database-authorized UTC day, so the adapter must call `permits_dispatch_at(now)` in that same
    pre-wire frame; permits are never retained across midnight. Each `dispatch_key` identifies one physical HTTP
    attempt and is globally unique within its app scope; it is bound to an opaque SHA-256 request fingerprint. A
    lost DB acknowledgement/crash replays as `already_authorized`, never as a second wire permit; a deliberate
    physical retry needs a new deterministic attempt key and conservatively consumes another daily charge. Audit
    outcomes are write-once and never refund quota.
  - **Scope and activation posture:** The quota scope is composition-owned, opaque, non-secret, and pinned by the
    owner-controlled singleton registry; a changed environment label fails closed until the migration owner changes
    that registry in the same deployment. It is never a tenant or request input. This increment does **not** add a
    Ticketmaster client, crawler, re-poller, API key, or claim that the unratified grid/reserve-sizing riders are live.
  - **Coverage:** Unit tests prove Pacer-wait/no-debit sequencing, a new permit only after Pacer grant, duplicate
    acknowledgement refusal, same-UTC-day permit validity, and audit forwarding. PostgreSQL tests prove exact class
    partitioning, atomic concurrent duplicate and distinct-key contention, immutable key binding, failure-no-refund
    behavior, database UTC selection, global key shape across day boundaries, rejected unconfigured scope, shared
    app-scope consumption across unrelated tenant GUCs, and app-role DML denial while the guarded function remains
    callable with no tenant GUC.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**209 unit, 40 integration; 122 source files**) against local Docker. No provider credential,
    external source request, RSVP, calendar write, payment, or commit was made. **NEXT: P1d Meetup app-scope
    fairness/degrade.**

- **2026-07-17 — P1d COMPLETE: G2-gated Meetup app-scope fairness and degradation.** `RedisPacer` now has an
  opt-in, opaque per-app Meetup key family containing a shared token bucket, active tenant ring, per-tenant FIFO
  lanes, item-expiry index, and deficit state. It uses one Redis Lua decision to enqueue/replay an item, refill or
  honor a shared backoff, rotate one DRR turn, and grant only the current lane head. Tenant UUIDs, queue identities,
  and the configured app label are SHA-256-derived before they appear in Redis keys or values; credential/token
  scopes remain unused in this optional app bucket.
  - **Admission and recovery semantics:** Pacer queue identities for membership read, registration-state read,
    mutation, and confirmation read are minted once in `RegistrationSagaKeys` and flow through the Temporal DTOs.
    A retry refreshes its existing FIFO item rather than duplicating it. A grant deliberately removes the advisory
    item, so a crash after a grant re-enqueues and conservatively re-charges before another source attempt; the
    existing source idempotency key still prevents a second RSVP effect. Expired Redis work is removed, and deleting
    fair state cold-starts at zero tokens—never a burst.
  - **Fairness and handoff posture:** the queue projection is conservative per item and is evaluated before source
    I/O. At exactly **300 seconds** it returns a durable `WAIT`; only `>300` seconds returns `DEGRADE`. Registration
    treats only that explicit P1d result as terminal, creates exactly one `HandoffReason.SATURATION` task/outbox row,
    and stops the parent candidate loop before any later candidate, RSVP, or calendar write. Existing `WAIT` and
    `SATURATED` paths retain their prior semantics; P1e owns browser saturation.
  - **G2 is still a hard gate:** defaults remain `EC_MEETUP_QUOTA_SCOPE_MODE=per_token`. Selecting `per_app` requires
    `EC_MEETUP_APP_G2_VALIDATED=true` and forces shared Redis even with mock cloud; otherwise composition fails
    closed. This flag is only a post-spike wiring guard—it adds no Meetup credential/client, live source call,
    measured point cost, or autonomous-lane SLA declaration.
  - **Coverage:** local Redis tests prove independent clients share one app scope despite distinct token labels, DRR
    rotation and per-tenant FIFO, strict 300/301-second behavior, retry-after sharing, script-safe fail-closed
    behavior, cold Redis loss, and conservative recharge after an advisory-grant crash. A Temporal/Postgres test
    proves a pre-mutation `DEGRADE` on candidate one produces one saturation handoff/outbox row, zero RSVP/calendar
    effects, and no candidate-two fall-through; it also proves paced retries reuse their minted fair-queue IDs.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**213 unit,
    46 integration; 122 source files**) against local Docker. No provider credential, external source request, RSVP,
    calendar write, payment, or commit was made. **NEXT: P1e browser admission cap.**

- **2026-07-17 — P1e COMPLETE: fenced browser-pool admission and fail-closed Luma routing.** A new
  `BrowserAdmissionPort` separates held physical browser capacity from ADR-005's disposable source-rate tokens.
  `InMemoryBrowserAdmission` remains the deterministic mock double; `RedisBrowserAdmission` is selected with the
  shared Redis control plane and uses one Lua decision over a global default **135-slot** pool. Its Redis members and
  fence owners are SHA-256 digests of workflow-stable lease IDs and fresh physical-holder fences, so tenant/workflow
  identifiers and fence tokens never enter Redis. A late cleanup can remove only its matching fence, never a renewed
  holder.
  - **Recovery and admission semantics:** Missing, partial, malformed, or unavailable Redis state is never treated
    as a free slot. The first atomic observer installs a full **five-minute** recovery fence—the ratified browser
    session wall-clock ceiling—and returns saturation; only after that fence can the clean pool admit work. Duplicate
    active stable IDs also saturate rather than authorize a second browser session, and crash-only cleanup occurs at
    bounded lease expiry. `EC_BROWSER_ADMISSION_LEASE_SECONDS` rejects values below 300 seconds, so an operator
    cannot shorten that loss fence below the known session lifetime. A matching completion releases immediately; a
    release-plane outage conservatively leaves the slot to expire.
  - **Saga and workflow semantics:** Every `Modality.BROWSER` registration-state read and Luma
    detect-then-submit call acquires browser admission first, then the ordinary source Pacer immediately before the
    source call, and releases in `finally` on success, rate wait, error, or lost acknowledgement. The already minted
    registration-read/mutation/confirmation IDs serve as stable lease identities while each physical attempt gets a
    fresh fence. Only `Lane.BROWSER_BEST_EFFORT` converts `SATURATED` into the terminal
    `HandoffReason.SATURATION` compensation; generic non-browser saturation behavior remains unchanged.
  - **Coverage:** Offline admission tests cover capacity, duplicate acquisition, matching and stale fenced release,
    expiry recovery, Redis outage, opaque Redis arguments, the ratified 135-slot default, and unsafe configuration
    rejection. Local Redis tests prove cross-worker capacity/release, cold recovery fencing, stale-release protection
    after renewal, and loss of each individual control structure. Temporal/Postgres Luma fixtures prove pool
    saturation creates exactly one handoff/outbox row with zero detect/submit/calendar effects and no candidate-two
    fall-through; a source-rate wait releases its browser slot before the durable timer, and a simulated browser ACK
    loss releases every held slot, reuses the stable read identity, and still submits exactly once.
  - **Activation posture:** This is an offline capacity/control-plane contract only. A real Browser/FleetPort,
    hard session teardown, dedicated browser task queue with ADR-003's 120-second ScheduleToStart bound, G3 relay
    validation, and a vendor/self-host capacity contract that supports the 135-slot watermark all remain prerequisites
    to live Luma RSVP activation.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**220 unit, 54 integration; 124 source files**) against local Docker. No provider credential,
    external source request, RSVP, calendar write, payment, or commit was made. **NEXT: P2 notifier wake-up and
    operational proof.**

- **2026-07-17 — P2 COMPLETE: push-woken, poll-safe notifier relay.** Migration `0045` installs a
  statement-level `AFTER INSERT` trigger that emits the fixed empty-payload `ec_outbox_ready` notification only when
  its enclosing outbox transaction commits. `PostgresOutboxWakeup` owns a dedicated autocommit psycopg listener
  connection rather than borrowing the SQLAlchemy transaction pool; a missing, failed, or restarted listener is
  advisory only and falls back to the existing bounded two-second durable poll before reconnecting on a later idle
  cycle. The trigger covers both direct outbox inserts and the guarded `fn_transition` insertion path without putting
  tenant/event data in a PostgreSQL notification.
  - **Relay operation and observability:** `NotifierWorker` starts the listener before its first durable queue probe
    and relay drain, immediately loops while rows are claimed, and waits only after an empty claim. Its immutable
    health/cycle projection treats a successful queue query as readiness even in `poll_fallback` mode, exposes
    listener failures/wakeups/fallbacks and cycle duration, and reports global opaque queue `pending`/`ready`/`leased`
    counts plus the oldest-ready timestamp using the exact `SKIP LOCKED` eligibility predicate. Worker logs label
    `NotificationPort` acceptance as `notifier_port_sends`, not user delivery; SES delivery timestamps, domain
    warm-up, and the 45s/90s delivery alert are still provider activation work.
  - **Coverage:** Offline listener tests prove autocommit setup, bounded connect/listen failure fallback, socket-loss
    cleanup/reconnect, and timeout validation. Controller tests prove listener-first ordering, no idle wait while
    draining, readiness in degraded poll mode, exact fallback interval, recovery on the next notification, database
    unready behavior, relay-fault backoff, and orderly close. PostgreSQL tests prove committed inserts wake the
    listener while rolled-back inserts do not, and queue snapshots correctly separate ready, expired lease, active
    lease, future, delivered, and failed rows. Existing PostgreSQL and crash/ledger tests continue to prove durable
    redelivery converges without a second user-visible notification (FR-6.6, FR-8.9, ADR-009).
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**235 unit, 56 integration; 127 source files**) against local Docker. No external provider,
    email, credential, source request, RSVP, calendar write, payment, or commit was made. **NEXT: P3 offline
    lifecycle and calendar contract completion.**

- **2026-07-17 — P3a COMPLETE: quiet lifecycle terminality.** The retained registration child, rather than the
  short-lived request parent, owns the event-end-plus-24-hour completion deadline. A reschedule replaces that
  deadline, and the guarded transition path makes the durable lifecycle completion and its outbox effect converge
  across crashes and retries. The work remains mock/fixture-backed and leaves the deferred D1–D8 attendance and
  out-of-band-verification product behavior untouched.

- **2026-07-17 — P3b COMPLETE: durable handoff-task terminality.** Migrations `0051` and `0052` bind each normal,
  calendar-recovery, and withdrawal handoff task to a once-minted expiry transition ID and a persisted
  `min(7 days, event start)` deadline. The retained child owns the normal TTL while the parent returns immediately
  after a terminal handoff directive. A task-driven expiration path is independent of catalog retention: the
  database verifies the exact task identity and due time, then atomically transitions lifecycle state, resolves the
  task and opaque queue row, writes the ledger, and emits the deduplicated `lifecycle.expired` notification outbox
  row. App-role direct handoff-task DML is revoked; the guarded creation path also prevents a delayed replay of an
  older task from superseding its newer successor. A liveness-gated opaque repair worker waits the five-minute
  eligibility grace and uses ADR-007's ratified 15-minute sweep cadence, so it cannot preempt a live retained
  workflow.
  - **Coverage:** Offline and local-Postgres/Temporal tests cover crash/retry exactly-once expiry effects, early
    expiry refusal, queue and RLS isolation, all three legal task states (`handoff`, calendar-recovery `registered`,
    and withdrawal `withdrawing`), missing-catalog terminality, stale replay protection, retained-child ownership,
    and notifier deduplication. Migration downgrade/upgrade checks cover `0052 → 0050 → head`.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**274 unit, 80 integration; 149 source files**) against local Docker. No provider credential,
    external source request, RSVP, calendar write, payment, or commit was made. **NEXT: P3c directive cleanup,
    recovered/calendar-recovery races, T+24-hour/T+5-day handoff reminders, and final failed-no-candidate cleanup;
    `/tasks/{token}/done` remains deferred with D6/FR-16.**

- **2026-07-17 — P3c COMPLETE: closed request/candidate terminality, recovery races, and durable handoff
  reminders.** The request parent now parks failed candidates only for the bounded attempt loop, selects the
  highest-ranked handoff-eligible candidate for its single terminal handoff, and atomically closes every declined
  child before returning. If discovery or all candidates yield no viable handoff, the guarded request terminal path
  writes the one `request.failed_no_candidate` audit/outbox effect. Candidate-close records remain deliberately
  notification-suppressed while a request is still advancing.
  - **Recovery/race safety:** `0053` adds request terminalization, while `0054` cancels a stale
    `calendar_write_failed` task and its repair-queue row when a factual lifecycle reaches `scheduled` or
    `reconciled`. Retained children accept queued organizer changes and un-RSVP commands before an active task TTL;
    Temporal patch markers preserve the historic directive and signal-only command histories. The normal parent
    remains short-lived and child ownership of post-booking lifecycle/expiry is unchanged.
  - **Reminders:** `0055` adds a private, RLS-forced task reminder ledger and the sole
    `fn_enqueue_handoff_reminder` guarded write path. The retained child derives T+24-hour and T+5-day timers from
    PostgreSQL's persisted task `created_at`, schedules only cadences strictly before the persisted TTL, gives user
    and organizer signals priority at a shared timestamp, and retries clock skew through a bounded durable timer.
    The SQL guard re-verifies tenant, task/lifecycle activity, exact identity, due time, and TTL under lock before
    atomically writing one `handoff.reminder` outbox row. The relay renders it as a deduplicated
    `NotificationKind.HANDOFF_REMINDER`; a lost activity acknowledgement returns `already_enqueued` rather than a
    second visible reminder.
  - **Coverage:** Local Temporal/PostgreSQL tests cover best-ranked handoff selection, no-result terminalization,
    candidate cleanup, calendar recovery interrupted by reschedule/un-RSVP, both reminder cadences/replay/no-early/
    inactive/RLS cases, and a post-commit reminder ACK loss with exactly one outbox effect. Unit relay coverage
    verifies reminder rendering/deduplication.
  - **Verification:** `make migrate && make test && make test-integration && make lint typecheck` passes (**280
    unit, 94 integration; 151 source files**) against local Docker. No external provider, credential, source
    request, RSVP, calendar write, payment, email delivery, or commit was made. **NEXT: P4 engine payload and
    trust-boundary contracts; `/tasks/{token}/done` remains deferred with D6/FR-16.**

- **2026-07-17 — P4a COMPLETE: bounded Temporal claim checks and raw-request history exclusion.** A new
  `ObjectStorePort` isolates opaque serialized Temporal payloads from workflow code; its filesystem-backed local mock
  is intentionally shared by independently started API, relay, and worker processes. The native Temporal external
  storage converter now offloads every payload at or above the ratified 256 KiB threshold to a tenant-scoped,
  content-addressed claim. A history reference carries only the canonical tenant UUID, opaque key, and SHA-256;
  retrieval validates the key shape and digest before decoding, unknown/non-deterministic workflow IDs fail closed,
  and the port already exposes tenant-prefix deletion for the future erasure service.
  - **History boundary:** Every Temporal client path (API, workflow worker, request-start relay, organizer-change
    fanout, and handoff-expiry repair) uses the same converter. `RequestInput` now carries only tenant/request IDs;
    `discover_and_rank` reopens the request through RLS at activity execution, so ordinary small request text cannot
    bypass the 256 KiB converter and enter immutable Temporal history.
  - **Coverage:** Unit contracts cover content-addressed claims, cross-process restore, parent/child deterministic
    identity validation, malformed claims, integrity mismatch, and fail-closed storage. A real Temporal engine test
    round-trips a >2 MiB input and proves its workflow history stays below 256 KiB; the request-spine regression
    proves raw intake text is absent from serialized history while ranking still succeeds through the persisted row.
  - **Verification:** `make migrate && make test && make test-integration && make lint typecheck` passes (**284
    unit, 95 integration; 154 source files**) against local Docker. No cloud account, provider credential, external
    source request, RSVP, calendar write, payment, email delivery, or commit was made. **NEXT: P4b local
    auth-context boundary and expanded RLS adversarial coverage; real OIDC/object-store/KMS activation remains
    owner-gated.**

- **2026-07-17 — P4b COMPLETE: injected API tenant context and adversarial RLS proof.** `AuthContextPort` is now
  the sole API-edge source of a tenant UUID. The local graph uses a deliberately test-only `X-EC-Tenant-ID` adapter
  that accepts exactly one canonical UUID and fails closed on absent, malformed, or ambiguous values; non-mock
  composition refuses to construct it and requires an injected OIDC/BFF adapter. Request, feed, and un-RSVP payload
  models forbid extra fields and no longer accept `tenant_id`, so a JSON body cannot choose an RLS context.
  - **Isolation coverage:** API regressions prove missing context yields `401`, a body tenant spoof is rejected,
    persisted request reads remain scoped to the authenticated tenant, and a tenant cannot signal another tenant's
    lifecycle. Database adversarial coverage now exercises handoff-task A/B isolation plus both unset and explicit
    empty-GUC fail-closed behavior, and proves the private request-terminal ledger/guard rejects foreign and empty
    contexts without producing an extra terminal outbox effect. Existing calendar binding/sync tests retain their
    equivalent RLS coverage.
  - **Boundary posture:** This establishes the swappable local/test contract, not a production authentication claim:
    real signed OIDC session resolution, tenant/admin identity-store hardening, and KMS-backed credential controls
    remain provisioned integration work. No external account or identity provider was created.
  - **Verification:** `make migrate && make test && make test-integration && make lint typecheck` passes (**293
    unit, 98 integration; 156 source files**) against local Docker. **NEXT: P4c secret-redaction and opaque
    short-lived-secret contracts; real OIDC/object-store/KMS activation remains owner-gated.**

- **2026-07-17 — P4c COMPLETE: opaque RelayInbox secrets and logging redaction.** The RelayInbox boundary now
  exposes `RelaySecretReference` metadata only—tenant, source, opaque UUID capability, allowlisted sender domain,
  and expiry. `EmailIngestionPort.await_secret_reference()` and the offline relay mock therefore cannot carry an OTP
  or magic-link plaintext. The InjectionBroker has the matching `fill_secret_reference()` capability handoff, so
  only the broker may redeem and type a consumed-once secret after domain pinning (FR-2.6, FR-5.7/5.8, ADR-011).
  - **Logging boundary:** a structlog processor now redacts sensitive fields recursively and scrubs common embedded
    secret shapes from exception strings and URLs before either local or JSON rendering. It deliberately retains
    opaque tenant/workflow correlation IDs; this is defense in depth, not permission to carry plaintext through
    application or workflow types.
  - **Coverage:** unit contracts validate relay tenant/source scope, the absence of plaintext fields, opaque broker
    redemption, malformed reference rejection, and redaction of nested values, credentials, bearer strings, and URL
    query secrets. The real Temporal and PostgreSQL suites remain green with the new contracts.
  - **Boundary posture:** this is an offline contract only. It does not activate an inbound relay, KMS secret store,
    relay-domain sending/receiving, or OTP workflow signal; G3 and the real broker remain owner-gated. No external
    account, provider credential, source request, RSVP, calendar write, payment, email delivery, or commit was made.
  - **Verification:** `make migrate && make test && make test-integration && make lint typecheck` passes (**300
    unit, 98 integration; 156 source files**) against local Docker. **NEXT: P4d offline external-artifact purge
    contract; do not represent it as full FR-10.5 erasure until retention and workflow-fencing decisions exist.**

- **2026-07-17 — P4d COMPLETE: bounded offline external-artifact purge contract.** `ExternalArtifactPurgeService`
  is deliberately a pre-erasure contract, not an account-erasure endpoint or an AC-74/NFR-11 completion claim. Its
  opaque inventory exposes only a tenant UUID and canonical-event UUID; the service derives the deterministic
  concierge calendar ID itself, rejects a foreign tenant target before any effect, deduplicates malformed inventory,
  then stages idempotent cleanup as **calendar → credential vault → claim-check object store** (FR-1.3, FR-9.2,
  FR-10.5, ADR-006/007/011).
  - **Failure posture:** the result exposes aggregate status/counts only—never an event ID, title, link, raw request,
    ciphertext, or provider exception. A calendar failure leaves vault/object-store artifacts intact so calendar
    access is not stranded; a later vault or object-store failure returns explicit `partial` rather than falsely
    reporting completion, and any replay uses the same deterministic deletes.
  - **Coverage:** offline tests cover two-tenant artifact isolation, foreign-inventory fail-closed behavior,
    duplicate-target convergence, replay, calendar/vault/object-store failure ordering, and PII-free result shape.
    The existing object-store prefix contract supplies the underlying cross-tenant purge proof.
  - **Explicit limit / owner blockers:** this does **not** purge RLS database request/lifecycle/history rows,
    RelayInbox contents, audit PII, calendar binding/sync state, notification/outbox control rows, or a real remote
    vault. It does not terminate or fence active Temporal/repair work that could recreate an artifact, and it has no
    legal-retention/tombstone policy. Those are required before an account-erasure API or FR-10.5/AC-74/NFR-11 claim;
    no external provider or account was activated.
  - **Verification:** `make migrate && make test && make test-integration && make lint typecheck` passes (**307
    unit, 98 integration; 159 source files**) against local Docker. **NEXT: P5a hermetic G1-style quality harness;
    synthetic coverage is not G1 closure or a provider/SLA claim.**

- **2026-07-17 — P5a COMPLETE: hermetic synthetic workflow-quality matrix.** Versioned fixture
  `tests/fixtures/quality/g1-v1.json` defines eight sanitized, Bay-Area-style request journeys: member Meetup
  autonomous registration, fixture-backed Luma browser registration, public-crawl and non-member handoffs, a hard
  conflict selecting its backup, paid/unknown normal-discovery visibility, explicit free-only exclusion, and an
  empty terminal result. The new `make quality` target runs the actual `EventRequestWorkflow` and
  `RegistrationWorkflow` activities against local PostgreSQL and Temporal's hermetic test server; every provider
  surface is an offline fixture/mock (P5, FR-4.6, FR-5.0, FR-6.6, FR-8.1, ADR-003/009).
  - **Determinism and scope:** each request is constrained to a narrow future fixture window so the persistent
    catalog cannot contaminate ranking. The matrix freezes retained-child timers while a parent returns, then derives
    a golden report containing only scenario IDs and aggregate outcomes, source families, mutation/calendar counts,
    notification kinds/dedup counts, and fault-overlays—never raw text, UUIDs, timestamps, URLs, or scores.
  - **Fault and notification proof:** the member-Meetup case loses a source acknowledgement, rejects a duplicate
    parent start while held at confirmation, then resumes from an opaque confirmation reference with exactly one
    RSVP effect. Committed outbox rows are read under only that scenario's tenant context, copied into a test-local
    relay queue, and replayed through the real `OutboxRelay` plus `MockNotifier`; one visible notification per dedup
    key remains. This supplements rather than replaces P2's durable global PostgreSQL relay tests.
  - **Boundary posture:** the corpus is deliberately synthetic and unweighted. It does not establish live source
    quality, latency p95, provider quota capacity, an autonomous Meetup SLA, G1 closure, G2, or G3; no source,
    browser, credential, calendar, or notification provider was contacted.
  - **Verification:** `make quality && make migrate && make test && make test-integration && make lint typecheck`
    passes (**307 unit, 99 integration; 159 source files**) against local Docker. **NEXT: P5b bounded deterministic
    repeat/load mode over isolated synthetic tenant namespaces, still without wall-clock SLA claims.**

- **2026-07-17 — P5b COMPLETE: bounded deterministic repetition/isolation mode.** `make quality-load` pins
  `EC_QUALITY_LOAD_REPEATS=5` and drives five serial cycles of the same eight synthetic journeys (**40** total) with
  fresh tenant/request/workflow IDs, mock ports, calendars, task queues, and hermetic Temporal environments. Each
  cycle must equal the P5a golden report exactly; the combined sanitized aggregate must equal five times every
  outcome/effect/dedup/fault count, all 40 tenant IDs must be distinct, and a tenant-B or empty-context app-role
  query must not see a known tenant-A request (P5, FR-1.3/1.4, FR-5.0, FR-6.6, FR-8.1, NFR-8, ADR-003/007/009).
  - **Bounded repeat semantics:** the repeat value is accepted only from 1 through 5, and ordinary
    `make test-integration` deliberately excludes the `quality_load` marker. Reusing the fixed corpus slots exercises
    catalog refresh/idempotency without growing a new global fixture catalog on every cycle. No `asyncio.gather` is
    used because workflow activities read a process-global composition container; a concurrent harness could
    cross-wire fixture adapters and would not be valid evidence.
  - **Boundary posture:** this demonstrates repeated semantic convergence and tenant isolation only. It establishes
    no throughput, p95, capacity, quota, live-provider, autonomous-Meetup, G1-closure, G2, or G3 result; no source,
    browser, credential, calendar, or notification provider was contacted.
  - **Verification:** `make quality-load && make quality && make migrate && make test && make test-integration &&
    make lint typecheck` passes (**307 unit, 99 ordinary integration plus 1 opt-in quality-load test; 159 source
    files**) against local Docker. **NEXT: only an owner-provided, consented realistic request corpus can turn this
    synthetic evidence into G1 measurement; external-provider gates remain held.**

- **2026-07-17 — P6a COMPLETE: immutable offline registration action-audit spine.** Migration `0056` adds
  `registration_action_audit`, a separate ledger from lifecycle `transition_ledger`. It retains only an opaque
  audit key, tenant/workflow identity, source/modality, one of the closed phases
  `policy_precheck | policy_pre_mutate | source_rsvp_outcome`, the allow/deny decision, a normalized settled
  source outcome when applicable, optional consent reference, and database timestamp. It deliberately stores no
  request text, title, URL, source-event ID, credential, OTP, provider payload, or free-form detail
  (FR-7.3, NFR-8/10, ADR-003/007).
  - **Guarded persistence and isolation:** `fn_append_registration_action_audit` derives tenant scope from the
    transaction-local context, validates the deterministic `{tenant}:{event}` workflow identity and legal phase
    combinations, serializes each opaque audit key, returns false only for an exact replay, and rejects a changed
    binding. RLS/FORCE-RLS uses the fail-closed `NULLIF(current_setting('app.tenant_id', true), '')::uuid`
    expression. The non-superuser `ec_app` role has SELECT only and function execution; direct INSERT, UPDATE, and
    DELETE are denied.
  - **Saga/recovery behavior:** the Temporal child passes its once-minted lane source key to both policy activity
    and RSVP activity, deriving stable `:policy_precheck`, `:policy_pre_mutate`, and `:source_rsvp_outcome`
    identities. An audit failure before the authoritative pre-mutate guard stops the RSVP wire mutation. A source
    effect followed by a lost activity acknowledgement, or a committed outcome fact followed by a lost audit
    acknowledgement, re-enters through the existing read-before-mutate path and converges on the same normalized
    outcome without a second RSVP.
  - **Evidence:** unit coverage proves the closed domain model, exact mock replay, allowed/denied paths, and
    fail-closed pre-mutate persistence. PostgreSQL coverage proves exact and concurrent replay, cross-tenant/unset/
    empty-GUC RLS isolation, app-role DML denial, and the PII-minimized schema. Real Temporal tests cover lost
    source ACK, lost audit ACK, and a policy flip between precheck and pre-mutate. The P5 corpus now reports only
    sanitized phase/count aggregates: its three registered journeys produce nine action facts, three in each phase;
    five isolated cycles preserve that aggregate exactly.
  - **Explicit boundary:** P6a initially wrote `consent_ref = NULL` and never treated absence as consent. P14c now
    requires an exact current opaque reference for every *new allowed* registration fact, while allowing only an
    exact replay of a historical nullable fact before current-evidence validation. It still does not implement
    trusted consent capture, account-linking, revocation/replacement, credential-access audit, confirmation-poll
    audit, retention/erasure policy, active workflow fencing, external providers, or a complete
    FR-2.9/FR-7.3/NFR-10 compliance program. Those need their own requirements/owner decisions rather than an
    inferred assertion.
  - **Verification:** `make migrate && make test && make test-integration && make quality && make quality-load &&
    make lint typecheck` passes (**312 unit, 105 ordinary integration plus 1 opt-in quality-load test; 163 source
    files**) against local Docker. **NEXT: re-rank the remaining offline requirements gaps without inferring
    consent, retention, OIDC, or provider-account decisions.**

- **2026-07-17 — P7a COMPLETE: durable, fail-closed policy control plane.** Migration `0057` turns the previously
  unused `source_policy` table into owner-controlled action policy, seeding the existing ratified launch matrix
  without overwriting a pre-existing operator row. It adds a singleton global freeze and RLS/FORCE-RLS per-tenant
  freeze, with the exact fail-closed `NULLIF(current_setting('app.tenant_id', true), '')::uuid` expression. The
  non-superuser `ec_app` role has read-only access; it cannot directly mutate source/global/tenant controls or call
  the owner-only control functions. Source automation JSON and signed-agent mode are structurally validated, and
  empty-payload commit-bound PostgreSQL notifications reserve the safe invalidation channel for a future listener
  cache without leaking tenant or policy contents.
  - **Data-plane behavior:** `StoreBackedPolicyEngine` reads a fresh PostgreSQL snapshot at every early and
    pre-mutate guard—stronger than ADR-004's permitted two-second propagation bound—and turns a missing/malformed
    snapshot or database error into a handoff denial. The static source map remains only the feed's advisory lane
    hint; it cannot authorize a stale source mutation. A startup `EC_KILL_SWITCH` remains an additional
    deny-only local fuse, while the database rows are the no-deploy operational mechanism (FR-5.9, FR-7.1/7.2,
    FR-10.1/10.3, AC-40/50, NFR-15, ADR-004).
  - **Evidence:** deterministic reader tests cover fresh tenant flips and store-loss denial. PostgreSQL coverage
    proves source quarantine, global and tenant freezes, owner-only mutation/function authority, malformed-policy
    rejection, and owner/other/unset/empty-GUC tenant-control visibility. Real Temporal coverage flips the durable
    global control after the early gate but before the source mutation, proving zero RSVP effect, and proves a
    policy-store outage becomes a handoff with one denied precheck audit fact. The synthetic one-cycle and five-cycle
    matrices retain their exact golden behavior.
  - **Explicit boundary:** RSVP-period counter/reset and failed-attempt charging semantics are unspecified and
    remain unimplemented; the concurrent-open cap is deferred D1 work. This does not implement an automatic
    ban/403 quarantine actuator, a policy listener cache, discovery permission changes, or B12's 30-minute
    freeze/recheck/resume fork; the existing immediate safe handoff behavior is preserved. No provider, account,
    credential, source request, RSVP, calendar write, payment, or commit was made.
  - **Verification:** `make migrate && make test && make test-integration && make quality && make quality-load &&
    make lint typecheck` passes (**314 unit, 110 ordinary integration plus 1 opt-in quality-load test; 165 source
    files**) against local Docker. **NEXT: P7b read-only lifecycle divergence/invariant scanner; production
    backup/restore, consent, retention, OIDC, and provider activation remain separately gated.**

- **2026-07-17 — P7b COMPLETE: read-only lifecycle divergence and invariant scanner.** Migration `0058` adds two
  fixed-search-path `SECURITY DEFINER` observability functions: a count-only snapshot over lifecycle, watch,
  handoff, queue, and projection hygiene, plus a bounded (1–1,000) opaque nonterminal-workflow keyset worklist for
  Temporal `describe` calls. The migration refuses to install unless the migration owner is a superuser or has
  `BYPASSRLS`, because these tables use FORCE RLS and a merely table-owning definer could falsely report a clean
  global scan. Runtime `ec_app` remains the non-superuser application role: it gets `EXECUTE` only on these narrow
  functions while its direct unset/empty tenant-context lifecycle reads still return zero rows.
  - **Observation behavior:** `LifecycleInvariantScanner` combines eleven PII-free database counts with a
    page-by-page read-only Temporal liveness check. It observes malformed deterministic
    `{tenant}:{event}` workflow identities, missing/mismatched active watches, terminal/orphan subscriptions and
    registries, active-handoff/expiry-queue drift, and pending watch projections. A closed/missing Temporal
    execution counts as divergence; transport failure or an incomplete liveness response counts as
    `uninspectable`, never as closed and never as permission to repair. The scan is deliberately a fuzzy
    observation while workflows continue to progress; it takes no locks and performs no state, queue, source,
    calendar, or notification mutation (ADR-007/008, NFR-8/10).
  - **Operations seam:** `make lifecycle-invariants` runs the continuous worker with a default 500-ID batch and
    24-hour cadence. It emits structured count-only clean/attention logs, including durable projection backlog;
    if Temporal is unavailable, it still emits the PostgreSQL snapshot and explicit uninspectable evidence rather
    than pretending the system is clean. The worker holds no queue lease and cannot become a sweeper authority.
  - **Evidence:** focused offline tests cover ordered paging, closed/open/uncertain accounting, bounded input,
    report/log PII exclusion, and missing Temporal status handling. PostgreSQL tests prove the direct-RLS contrast
    under unset and empty GUCs, privileged-definer ownership, app `EXECUTE` authority, aggregate/opaque function
    output shape, valid registered lifecycle missing-watch detection, malformed deterministic workflow-ID
    detection, inactive handoff-expiry queue detection, adapter mapping, and fixture cleanup. The migration was
    replayed locally after its final SQL hardening so this evidence exercises the checked-in function body.
  - **Explicit boundary:** structured logs are an operational alert seam, not a provisioned PagerDuty/SES paging
    adapter or a production schedule deployment. This does not establish NFR-13 backup/PITR, restore-drill RPO/RTO,
    real provider activation, consent/retention/OIDC, or any deferred v0.3 delta. No external provider, account,
    credential, source request, RSVP, calendar write, payment, or commit was made.
  - **Verification:** `make migrate && make test && make test-integration && make quality && make quality-load &&
    make lint typecheck` passes (**325 unit, 112 ordinary integration plus 1 opt-in quality-load test; 172 source
    files**) against local Docker. **NEXT: re-rank the remaining ratified offline-safe gaps; production recovery
    infrastructure and external paging remain owner/infra-gated.**

- **2026-07-18 — P7c COMPLETE: durable ban/403 circuit breaker and discovery dispatch gate.** Migration `0059`
  installs `fn_quarantine_source(source, signal)`: a fixed-search-path, SECURITY DEFINER, app-role capability that
  accepts only a known source plus the closed `ban | forbidden` signal and can make exactly one monotonic policy
  transition, `quarantined = false -> true`. It preserves the source's automation, paid, and signed-agent controls;
  replay returns an idempotent false result; runtime `ec_app` has execute only and cannot clear a quarantine,
  update policy tables, or call the owner-only release function. Existing policy-change notifications still fire on
  the transition (FR-10.3, AC-72, ADR-004).
  - **Dispatch/recovery behavior:** adapters surface only `SourceAccessDeniedError(BAN|FORBIDDEN)` at the port
    boundary. Meetup's fixture-backed scaffold keeps `401 -> reconsent` and maps `403 -> forbidden`; it remains
    disabled until the owner-run G2 gate, with no live provider activation. The registration saga now checks fresh
    policy before membership, RSVP-state, confirmation, and mutation calls, then acquires the Pacer/browser lease
    immediately before the source operation. A raw denial first requests the durable flip and returns a typed,
    non-exceptional source-quarantined result; the Temporal child and local facade route that result directly to one
    handoff instead of trying another autonomous lane. Withdrawal read/mutate paths use the same fence and fail
    safely to their existing manual-withdrawal handoff.
  - **Catalog/discovery behavior:** a distinct fresh source/modality gate protects tenant-neutral discovery and
    catalog refresh. It denies policy-store loss, unknown rows, disabled modality, and quarantine before source
    dispatch; catalog refresh repeats the check immediately before fetch after its Pacer decision. A crawl ban
    writes only the generic failed-run reason, records no raw provider response, and makes the next dispatch stop
    before Pacer/fetch. The default durable `public_jsonld` row intentionally remains `{}`: ordinary production
    crawling therefore stays fail-closed until the owner explicitly enables its reviewed `browser` modality through
    the owner-only policy control. Offline slice/quality fixtures inject their explicit approval instead; this
    increment did not silently enable crawling.
  - **Evidence:** focused unit contracts cover monotonic mock behavior, 403 normalization, membership/read/mutation
    fences, direct-facade handoff, discovery/catalog first-ban plus zero-subsequent-provider-call behavior, and safe catalog
    persistence. PostgreSQL tests prove app-role execute-only authority, invalid-signal rejection, idempotence, and
    owner-only release. A real Temporal/PostgreSQL ACK-loss test commits the quarantine, crashes the activity before
    acknowledgement, retries through the fresh policy fence, and proves exactly one source read/quarantine
    actuation, zero RSVP/calendar effects, one direct handoff/outbox row, and no second Pacer/provider request.
  - **Explicit boundary:** this is a safety mechanism, not a live-provider assertion. It does not create a Meetup
    Pro OAuth consumer, relay domain, browser identity, IP-rotation path, Luma live driver, owner policy release,
    source account, credential, or live source request. Supply-risk recording, listener-cache monitoring, RSVP
    period counters, D1-D8 behavior, and production backup/PITR remain separately scoped work.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**340 unit, 114 ordinary integration; 176 source files**) against local Docker; `make
    quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repeat test). No external
    provider, account, credential, source request, RSVP, calendar write, payment, commit, or owner policy release
    was made. **NEXT: re-rank the remaining offline-safe gaps; any production public-crawl enablement is an
    explicit owner policy operation.**

- **2026-07-18 — P8a COMPLETE: durable, revisioned tenant ranking profiles.** Migration `0060` adds
  `tenant_ranking_profiles`: one current profile row per tenant, with JSONB explicit and implicit affinity maps,
  a nonnegative monotonic revision, timestamps, a tenant foreign key, and validation that database maps are JSON
  objects with nonblank labels and numeric values. It uses FORCE RLS with the exact fail-closed
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` tenant predicate. Runtime `ec_app` receives only
  SELECT/INSERT/UPDATE; deletion remains owner-only.
  - **Profile contract:** `UserRankingProfile` copies caller maps into immutable, trimmed-label, finite
    non-boolean-number maps before an adapter can serialize them. `RankingProfileUpdate` requires a
    caller-minted positive revision. The PostgreSQL conditional upsert and the offline
    `InMemoryRankingProfiles` double share one outcome contract: a higher revision is `APPLIED`, an exact
    same-revision/profile retry is `REPLAYED`, a different profile at the same revision is rejected, and a lower
    revision is `STALE` and returns the current profile/revision without changing it. This prevents a delayed
    at-least-once delivery from overwriting a newer ranking state without inventing a feedback merge policy.
  - **Composition and cold start:** the normal composition root now gives `PersonalizedRanker` the PostgreSQL
    repository, while tests can inject the deterministic in-memory repository. A missing tenant row resolves to
    neutral affinities, preserving the existing no-error cold-start ranker behavior.
  - **Evidence:** unit contracts cover normalization/immutability, malformed/nonfinite rejection, positive
    revisions, and all applied/replayed/conflicting/stale outcomes. PostgreSQL integration tests prove owner/other/
    unset/empty RLS contexts, app-role map-constraint rejection, replay/stale convergence, conflicting same-
    revision rejection, and app-role delete denial. The same-candidate persisted-profile integration also runs
    `PersonalizedRanker` through the PostgreSQL repository and proves a stored affinity changes the deterministic
    re-score order. No account, credential, provider request, source crawl, RSVP, calendar write, payment, or
    commit was made.
  - **Explicit boundary:** this stores current already-aggregated affinity inputs only. It does not add feedback
    capture, click/dwell semantics, preference/onboarding UI or API, profile erasure/retention completion,
    cohort/model training, decay/weight policy, a Cohere activation, or any deferred requirements-v0.3 delta.
    Those require separate product/owner decisions where applicable.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**358 unit, 120 ordinary integration; 177 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P9a COMPLETE: FORCE-RLS tenant identity and guarded provisioning.** Migration `0061` converts
  `tenants`—the original pre-RLS onboarding exception holding OIDC subject, notification email, and inbound
  RelayInbox binding—into a FORCE-RLS table using the exact fail-closed
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` predicate. Runtime `ec_app` now has SELECT only:
  direct INSERT, UPDATE, and DELETE are denied even for the row selected by its tenant context (FR-1.2--FR-1.4,
  AC-1/AC-3, NFR-7, ADR-001).
  - **Provisioning boundary:** `fn_provision_tenant(uuid, text, text, text)` is the app role's sole identity
    write capability. It has a fixed `pg_catalog, public` search path, executes as a migration owner that must be
    superuser or `BYPASSRLS`, serializes the immutable tenant/subject/relay bindings with transaction advisory
    locks, returns `true` for a new binding and `false` for an exact retry, and rejects both changed same-tenant
    input and reused external bindings with one generic conflict message. It does not leak the existing or
    attempted subject, email, or relay value. `PostgresTenantRepository.add` preserves the existing async
    onboarding facade by calling that capability; `get` opens the supplied tenant's normal `SET LOCAL` RLS
    context. The local mock `/v1/onboard` surface and demo therefore retain their call shape while no longer
    receive raw table-write authority (FR-1.5/AC-4, ADR-011).
  - **Evidence:** real-PostgreSQL tests prove tenant A/B isolation, omitted and explicitly empty tenant GUCs
    returning zero identity rows, direct DML denial, new/exact-replay/conflicting provisioning behavior, and
    conflict-message PII exclusion. Existing calendar-binding, calendar-sync, handoff-expiry, and lifecycle
    fixtures now seed tenants through the same capability; the full integration suite also exercises API,
    policy-control, opaque queue, and foreign-key regression paths under the non-superuser `ec_app` role.
  - **Explicit boundary:** this is a database capability hardening, not real OIDC authentication or subject
    verification, RelayInbox-domain allocation/acceptance (G3), consent/credential security, production
    provisioning operations, or full erasure/retention. It enforces the zero-row half of AC-3; a missing-context
    policy-violation audit event remains separate work. The pre-existing ADR-008 opaque global queues also retain
    their separately scoped foreign-key existence-side-channel risk. No external provider, account, credential,
    source request, RSVP, calendar write, payment, commit, or owner decision was made.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**358 unit, 125 ordinary integration; 177 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P10a COMPLETE: deterministic, bounded catalog cadence dispatch.** The registry's persisted
  `refresh_interval_minutes` is now an executable control-plane input. `PostgresCatalogSourceRepository` derives
  a reviewed source's due time from its latest successful `catalog_refresh_runs` completion plus that interval;
  before any success, the owner-review timestamp is the cursor. A successful manual refresh therefore suppresses
  the next cadence slot exactly as a scheduled success would; a failed or still-running slot does not advance it
  (FR-3.3, NFR-1/NFR-8, ADR-001).
  - **Dispatch contract:** `CatalogCadenceDispatcher` selects at most the configured batch of due sources in
    deterministic due-time/source-key order and calls the existing `CatalogRefreshService` sequentially with
    `cadence:{source}:{UTC-slot}`. The stable slot key makes crash/retry and multiple dispatcher instances converge
    through the already-proven `(source_key, run_key)` lease; a generic service exception is reduced to a source
    key plus error class so unreviewed provider text is not surfaced by worker logs. `make catalog-cadence` exposes
    one explicit pass only. It never sleeps, creates a Temporal Schedule, or bypasses the Pacer, approved-origin
    ACL, source-quarantine actuator, or fresh immediately-pre-fetch policy re-read (FR-3.9, NFR-8, ADR-004/005).
  - **Evidence:** unit contracts prove stable ordering/key minting, batch bounding, isolated-source failure
    continuation with no error-text leak, and rejection of a naive scheduling clock. PostgreSQL coverage proves
    an initial reviewed cursor, a manual-success interval suppression boundary, exact next due slot, and a failed
    slot retaining the same retry key. A worker smoke set `EC_DISCOVERY_SOURCES=` empty, so the due source was
    skipped for lack of a fetcher and could not make an external request.
  - **Explicit boundary:** no recurring Temporal Schedule/cron deployment, public-GET policy enablement, live
    crawl, Ticketmaster/SeatGeek adapter or geo/category cell planner, SerpApi sweep, budget claim, provider
    credential, RSVP, calendar write, or payment was added. An owner must explicitly authorize both any recurring
    production public-GET cadence and its reviewed source-policy enablement. This is general cadence plumbing, not
    an NFR-1 ≤6h, AC-21, or organizer-change NFR-17 claim; ADR-001's source-specific launch schedule remains
    separately scoped.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**362 unit, 126 ordinary integration; 179 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P11a COMPLETE: durable, fixture-only organizer-change watch polling.** Migration `0062` adds
  `watch_poll_state`, a public distinct-watch timing/lease cursor keyed by canonical event and source. It holds
  only timestamps, counters, an opaque lease token, and a bounded exception-class/code token—never tenant
  identity, contact data, source URL, raw provider response, or error text. `PostgresChangeDetectionRepository`
  now carries the stable `watch_registry.created_at` baseline; a first state preserves that registration age for
  staleness while being immediately eligible for its first poll. The non-superuser app role performs cursor work
  through its granted control-plane access, while watch membership remains in ADR-008's guarded projection
  functions (FR-8.7a, NFR-17, ADR-008).
  - **Scheduling and recovery contract:** `WatchPollPlan` accepts only caller-supplied typed cadence values—there
    are intentionally no Luma/Meetup/Ticketmaster defaults while B24/B25 remain unratified. The bounded scheduler
    orders unpolled/oldest-due watches fairly, then completes one exact lease at a time: fixture poll, source-bound
    observation validation, durable `event_changes`/delivery record, and only then cursor acknowledgement. A
    lost acknowledgement or worker crash can re-read the public event, but fingerprinted ledger insertion preserves
    one change and one opaque workflow delivery. Failed polls retain the watch and record only the error type;
    stale lease holders cannot acknowledge or release a newer attempt.
  - **Fanout and visibility boundary:** poll success depends on the durable ledger, not a later Temporal signal.
    `record_observations()` separates that durable point from the existing independently leased fanout drain, so a
    fanout outage cannot produce a false source-poll failure. The cycle returns both preflight and postflight health:
    an overdue cursor remains observable at recovery time even if the recovery poll succeeds. Health distinguishes
    new pending watches from aged missing cursors, absent source plans, stale success, and consecutive-failure
    alarms; a future worker/metric sink must emit the preflight alarm.
  - **Evidence:** fixture tests cover one shared public poll for two tenant subscriptions, empty-success cadence,
    failure alarms without watch removal, registration-age staleness, loss of a post-ledger cursor acknowledgement
    with one durable change/delivery after replay, fair bounded-pass selection, and preflight stale visibility on
    recovery. PostgreSQL coverage proves a second lease after expiry rejects the first lease's stale success/failure
    acknowledgement and advances only the current cursor. The global registry reader also ignores zero-subscriber
    orphan rows, so an interrupted test/control-plane fixture cannot consume detector capacity.
  - **Explicit boundary:** no live provider detector, HTTP/browser request, Pacer lease, Temporal Schedule/cron,
    polling worker, webhook/RelayInbox feeder, source-specific cadence, alert sink, SLO/metric deployment, or
    production coverage assertion was added. Before activation, the owner must ratify B24/B25 and the relevant
    G1/G2 sizing/economics; implementation must add a source-specific paced due-batch query, provider timeout/lease
    policy, capacity/priority plan, and operational alarm emission. This does not claim NFR-17 or AC-60.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**371 unit, 127 ordinary integration; 181 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P12a COMPLETE: guarded ADR-008 global change-control plane.** Migration `0063` replaces direct
  `ec_app` access to `event_changes`, delivery/repair queues, `lifecycle_organizer_change_ledger`, lifecycle watch
  projections, and tenant/workflow-bearing `watch_subscriptions` with narrowly shaped `SECURITY DEFINER`
  capabilities. Every function has `pg_catalog, public` fixed search path; PUBLIC execution is revoked and only
  `ec_app` receives the required execute grant. The app role also loses direct access to the two queue identity
  sequences, eliminating raw queue-volume reads and direct sequence advancement (NFR-7/NFR-8, ADR-007/008).
  - **Capability contract:** `fn_record_event_change` validates public normalized input and a real public source
    link, takes the same canonical-event advisory lock as watch registration, timestamps only after that lock, and
    atomically fans one fingerprint to every canonical-event subscription. Its generic `(false, 0)` result covers
    duplicate, malformed, or missing public-link input rather than exposing foreign-key detail. A separate aggregate
    watch-list capability returns only canonical event, source, registration time, and subscriber count; the app
    can no longer enumerate tenant UUID/workflow pairs from `watch_subscriptions`.
  - **Lease/recovery contract:** delivery, delayed calendar-repair, and lifecycle-watch-projection claim functions
    retain `SKIP LOCKED`, cap lease batches at 1,000/one hour, reject null or malformed bounds, and return only the
    lease projection each worker needs. Acknowledgement/release requires the exact current token. Delivery and repair
    backoff is calculated from the stored attempt count inside the database; an app caller cannot shorten it.
    Enqueuing a closed-workflow repair retires exactly the held delivery and derives its fingerprint/tenant/workflow
    from that row in the same transaction. The repair worker's ledger query is scoped to an exact held repair lease,
    so it cannot probe arbitrary tenant/workflow/fingerprint identities.
  - **Evidence:** PostgreSQL tests prove all raw table DML/read and queue-sequence privileges are absent while all
    thirteen capabilities remain executable; invalid/unbounded input fails closed. Existing change fanout tests now
    use owner-only clock fixtures where they intentionally manipulate durable time. New real-PostgreSQL adapter
    contracts exercise calendar-repair and watch-projection enqueue/claim/retry/ack flows, including stale-token
    rejection, alongside the existing record/fanout crash/retry coverage.
  - **Explicit boundary:** this is local database authorization hardening only. It does not activate a source poll,
    provider call, webhook, Temporal schedule, alert sink, RSVP, calendar mutation, or external account. The
    public-only `watch_poll_state` cursor is hardened separately in P12b; the required missing-tenant-context audit
    event (FR-1.4/AC-3) is a separate tenant/session-boundary design slice.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**371 unit, 130 ordinary integration; 181 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P13a COMPLETE: explicit missing-tenant-context audit boundary.** Migration `0064` adds
  `tenant_context_violation_audit`, a private, PII-free global evidence table containing only a fixed violation
  type, the fixed runtime caller role, and a timestamp. The non-superuser app role has no table or identity-sequence
  privileges; its sole write path is the no-argument, fixed-search-path `SECURITY DEFINER` capability
  `fn_record_missing_tenant_context()`. That capability rejects any nonempty tenant GUC, so a valid tenant cannot
  fabricate a missing-context event (FR-1.3/FR-1.4, AC-3, NFR-7, ADR-001).
  - **Session-intent contract:** `tenant_session_scope(tenant_id)` now has no implicit global mode. Every tenant
    transaction explicitly sets its transaction-local `app.tenant_id`; `tenant_session_scope(None)` first commits
    the private event in its own transaction, then yields a separately cleared-GUC target transaction. Thus a
    zero-row RLS read and a rejected RLS write both retain one policy-violation event. Intentional catalog, opaque
    queue, provisioning, and other tenant-neutral work uses `system_session_scope()`, which explicitly clears the
    GUC on every transaction and does not create a false violation. This also eliminates pooled-connection
    inheritance of a stale session-level tenant setting.
  - **Evidence:** real PostgreSQL tests prove a no-context tenant read returns zero rows and adds exactly one
    owner-visible event; a subsequent RLS write rejection cannot roll that event back; valid tenant and system
    scopes do not write one; the app role has no audit-table/sequence privilege but can execute the constrained
    function; and a real tenant context cannot call it. The existing RLS suite now exercises the explicit scope
    distinction throughout adapters, workers, API fixtures, and control-plane paths.
  - **Explicit boundary:** this is deliberately an application-managed session boundary, not a claim that
    PostgreSQL can emit one reliable event for arbitrary raw `SELECT` statements (it has no SELECT triggers and
    RLS predicates are plan/row dependent). A malicious direct database client, an operator SQL-audit extension,
    read-replica audit routing, alerting/retention policy, and API/OIDC authentication rejection events remain
    separate work and were not silently introduced. No source call, provider credential, RSVP, calendar mutation,
    payment, external account, commit, or owner decision was made.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**371 unit, 134 ordinary integration; 181 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-18 — P12b COMPLETE: guarded public watch-poll cursor.** Migration `0065` removes the application
  role's direct `SELECT`/`INSERT`/`UPDATE`/`DELETE` authority over the public-only `watch_poll_state` cursor.
  The table has a composite key rather than an identity sequence; the app now has only four fixed-search-path
  `SECURITY DEFINER` operations: `fn_claim_watch_poll`, `fn_mark_watch_poll_succeeded`,
  `fn_release_watch_poll`, and `fn_list_watch_poll_states` (FR-8.7a, NFR-7/NFR-8, ADR-008).
  - **Cursor/lease contract:** claim validates a known source, aware caller-supplied fixture time, a 1--3,600s
    lease, and a bounded opaque token. It initializes only from the existing `watch_registry` key when at least
    one guarded lifecycle subscription remains, preserving the registry's first-seen timestamp while refusing
    a zero-subscriber/orphan key. Success and failure require the exact current lease token, preserve the
    caller-supplied forward-only cadence slot (no unratified source interval is invented), and retain only a
    bounded class/code failure token. There is deliberately no direct-delete or arbitrary-cursor-update path.
  - **Health projection:** the list capability returns only public timing/failure facts. It redacts an active
    lease token and expiry from the read model, because staleness assessment does not need a mutable
    acknowledgement secret. The claiming worker receives its token only in the exact claim response.
  - **Evidence:** real PostgreSQL tests prove raw table read/DML denial and all four executable capabilities;
    malformed/oversize claims fail closed; a public registry key with no subscriber cannot create work; an
    active watch initializes from the registry age, reclaims after expiry, rejects stale success/failure
    acknowledgements, records a bounded failure, and hides live lease credentials from state reads. The offline
    adapter's local validation also rejects leases over one hour.
  - **Explicit boundary:** this is public control-plane authorization hardening only. It does not activate a
    poller, source request, Pacer lease, Temporal schedule, provider cadence, alert sink, RSVP, calendar write,
    credential, or external account. The still-owner-gated B24/B25 cadence and G1/G2 sizing/economics are not
    inferred from this generic cursor; live detector activation remains separate work.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**371 unit, 136 ordinary integration; 181 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-17 — P14a COMPLETE: reviewed-source and catalog-refresh capability boundary.** Migration `0066`
  makes the owner-reviewed registry the actual authority it was always meant to be. `ec_app` retains only
  `SELECT` on the public, non-secret `catalog_sources` metadata; it has no direct mutation route to introduce an
  arbitrary HTTPS origin, redirect an approved source, alter review/enable state, or suppress a source. The
  opaque `catalog_refresh_runs` table is now entirely inaccessible to the app role, including raw lease/error
  reads (FR-10.3, NFR-8, ADR-001/004).
  - **Capability and clock contract:** fixed-search-path `SECURITY DEFINER` functions list due slots, read one
    exact run, and claim/complete/fail a bounded source/run identity. Claims accept only a known currently
    reviewed, enabled, handoff-only source, a bounded key/token, and a 1--3,600-second lease; database time owns
    the run start, lease expiry, and terminal timestamp. Completion/failure require the exact active token, cap
    error text, reject invalid counts, and preserve one failed-or-expired-run reclaim / completed-run convergence.
    Static reviewed-source changes are migration-owner work, not a new runtime admin surface.
  - **Pre-fetch authority fence:** after Pacer acquisition, `CatalogRefreshService` reloads the source record and
    its adapter before the existing immediate policy re-read. A removed, disabled, unreviewed, expired,
    non-handoff, unsupported, or temporarily unreadable record releases the held run and makes zero source call;
    an authorized owner revision that remains valid becomes the fetcher's current source record. This adds no
    scheduler, recurrence, fetch, provider credential, or change to Pacer policy.
  - **Evidence:** unit coverage freezes a source-disable during Pacer acquisition and proves no fetch/catalog
    write. PostgreSQL coverage proves static-registry read-only privilege, no raw refresh-run read/DML, all five
    capabilities executable, malformed input failing closed, an unreviewed source unable to create a run, stale
    acknowledgement rejection, failed-run reclaim, completed-run convergence, and database-clock cadence cursor
    behavior. Test fixtures seed reviewed sources only through the migration-owner connection.
  - **Explicit boundary:** this removes broad table authority, not the legitimate worker ability to invoke its
    own narrowly granted operations. It does not create a source-review workflow, activate any existing approved
    publisher, make a public HTTP call, add raw-source replay, provision a Temporal schedule, or decide recurring
    production cadence. RelayInbox ingress, live relay-domain/KMS/SES work, G3, provider integrations, and v0.3
    deltas remain separately scoped.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make
    lint typecheck` passes (**372 unit, 138 ordinary integration; 181 source files**) against local Docker;
    `make quality` and `make quality-load` also pass (one hermetic matrix plus one bounded repetition test).

- **2026-07-17 — P14b COMPLETE: fixture-only RelayInbox ingress and opaque local handoff.** A new local adapter
  accepts only bounded RFC 822 fixture bytes plus trusted fixture transport metadata. It resolves a fixed
  recipient-to-tenant binding, matches exactly one source rule through a true-domain/subdomain allowlist (never a
  suffix lookalike), and extracts exactly one artifact class with deterministic keyword-plus-length OTP rules or
  explicit visible HTTPS magic-link rules. The HTML path retains visible text only and drops script, style,
  template, noscript, SVG, attributes, attachments, malformed content, oversized content, untrusted recipients,
  spoofed senders, expired deliveries, unsupported links, and ambiguous OTP/link inputs (FR-2.6, FR-5.7/5.8,
  FR-10.6, ADR-011).
  - **Opaque handoff contract:** `RelaySecretReferenceStorePort`, publisher, and redemption ports carry only a
    tenant/source-scoped UUID capability plus sender-domain and expiry metadata. The metadata-only fixture registry
    deliberately cannot store an OTP, magic link, MIME body, ciphertext, or raw HTML; it mints one reference per
    delivery, treats an exact live redelivery idempotently, expires it, and allows exactly one broker redemption.
    The fixture publisher provides one bounded tenant/source slot, accepts a replay of the same reference without
    duplicating it, and dequeues it once. The mock broker checks a nonblank domain pin before it consumes a supplied
    registry reference, preserving the capability on a bad origin.
  - **Evidence:** unit fixtures prove opaque OTP and magic-link classification, non-leakage through results and
    references, spoof/unmapped/malformed/hostile/ambiguous rejection before publish, delivery deduplication,
    tenant/source isolation, expiry, and consumed-once behavior. Existing opaque-reference contracts continue to
    cover the legacy canned workflow seam.
  - **Explicit boundary:** this is not a real RelayInbox. It creates no relay address/domain, SMTP/SES/S3/KMS
    estate, provider trust/authentication, plaintext secret store, workflow signal/correlation, browser action,
    notification, source call, account linkage, or G3 closure. It uses only `*.fixture.test` rules; real
    Luma/Eventbrite/Meetup templates, relay acceptance, per-provider TTLs, durable signal correlation, encrypted
    production storage, erasure, and broker implementation remain separately gated work.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**384 unit, 138 ordinary integration; 182 source files**) against local Docker; focused
    `pytest tests/unit/test_relay_inbox.py tests/unit/test_fixture_relay_inbox.py -q` passes (**17 tests**) with
    focused Ruff/format and strict mypy. No migration, external account, provider request, relay-domain action, or
    commit was made.

- **2026-07-17 — P14c COMPLETE: owner-seeded registration-consent evidence and pre-action fence.** Migration
  `0067` adds `tenant_source_consents`: an immutable, tenant/source/modality-bound, fixed-`registration`-scope
  registry under FORCE RLS with the fail-closed
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` predicate. `ec_app` has no raw table SELECT or DML;
  it receives only root-pinned `SECURITY DEFINER` resolve/validate capabilities, each deriving tenant identity from
  the transaction GUC. Fixture evidence is seeded solely through the migration-owner connection after a tenant is
  provisioned; application code has no grant, replacement, revoke, or inference operation (FR-2.9, NFR-7/10).
  - **Registration and audit fence:** only the ratified current autonomous tuples, Meetup/API and Luma/browser,
    are representable; the PostgreSQL adapter and offline mock reject every other pair. Every membership, RSVP,
    and confirmation source read resolves evidence before its policy, Pacer, browser, or provider-I/O boundary.
    A missing Meetup membership lookup retains only its advisory lane long enough for the next policy activity to
    append the explicit denied precheck; it cannot spend a Pacer permit or make a source call. The RSVP mutation
    validates the same reference again immediately after its Pacer grant and before the adapter call. Missing or
    unavailable evidence becomes a handoff with zero source effect. Every new allowed precheck, pre-mutate, and
    source-outcome fact carries a matching non-null opaque ref. The audit guard preserves P6a ACK-loss convergence
    by checking an exact existing key before current-consent validation, so an historical nullable row can still
    replay exactly while a newly invented, foreign, mismatched, or absent reference is rejected (FR-5.3, FR-7.3,
    NFR-8/10, ADR-003/004/005/007).
  - **Evidence:** unit coverage proves exact tenant/source/modality mock binding, rejection of impossible fixture
    tuples, no-consent zero source activity, a confirmation wake-up blocked before Pacer/source I/O, a post-Pacer
    evidence loss preventing the RSVP effect, same-ref audit linkage, and a missing-evidence denied precheck.
    PostgreSQL coverage proves owner-seeded resolver/validator isolation for owner/other/unset/empty tenant
    contexts, app-role raw table denial, invalid-reference rejection, and historical-null exact replay. A real
    Temporal no-consent child regression now proves the normal workflow writes exactly one
    `policy_precheck`/`denied`/`NULL` fact then hands off without a Pacer or provider effect. Existing real Temporal
    lost-ACK/crash/retry coverage remains green with explicit migration-owner fixture seeds, preserving exactly one
    source mutation, and proves the seeded opaque consent reference never enters serialized Temporal history; slice
    and G1 fixture registration lanes do the same.
  - **Explicit boundary:** this is not trustworthy user consent capture or account linking. It creates no OAuth,
    browser login, credential, real RelayInbox, provider request, calendar authorization, withdrawal authority,
    disconnect/re-consent flow, retention/erasure policy, scope hierarchy, replacement/revocation semantics, or G3
    claim. Broadening it requires an owner-ratified trusted capture/disconnect contract; no such authority is
    inferred here.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**390 unit, 142 ordinary integration; 185 source files**) against local Docker. Focused
    consent unit and PostgreSQL suites, the full Temporal workflow suite, the vertical slice, and the hermetic G1
    matrix all pass. No external account, provider, domain, or relay action was taken, and no commit was made.

- **2026-07-17 — Bay Area publisher source-review pass: SFJAZZ, SF Symphony, and Exploratorium held.** These
  popular public calendars were reviewed read-only before any registry or adapter change; none is being treated as
  permission to crawl merely because a list page exists.
  - **SFJAZZ:** [`/calendar/`](https://www.sfjazz.org/calendar/) is a browser-oriented month-scrolling list and
    [`robots.txt`](https://www.sfjazz.org/robots.txt) does not itself exclude it, but the current
    [Terms](https://www.sfjazz.org/about/terms-of-service/) prohibit bots, spiders, site-retrieval tools,
    data-mining, and systematic database creation. Its current Cloudflare managed challenge is a further stop
    signal. Hold unless SFJAZZ provides written permission or an official feed; never solve or bypass the challenge.
  - **SF Symphony:** public detail pages and a season PDF exist, but the current calendar/sitemap route redirects to
    a JavaScript-required waiting room. No documented event-list API or safe pagination contract was identified,
    and the operative reuse terms could not be independently verified through that boundary. Hold for an authorized
    feed or written permission; never automate the queue or ticket surface.
  - **Exploratorium:** [`/visit/calendar`](https://www.exploratorium.edu/visit/calendar) is a bounded,
    server-rendered listing and [`robots.txt`](https://www.exploratorium.edu/robots.txt) permits it, but the
    [Use Policy](https://www.exploratorium.edu/about/use-policy) limits site use/assets to noncommercial purposes
    and requires prior written permission for commercial reproduction/distribution. Hold live ingestion pending
    written confirmation; a fixture-only parser remains technically possible but provides no catalog coverage.
  - **Next:** continue source review only for a publisher with a public list/feed, robots-compatible access, and
    reuse posture that fits the existing manual, read-only, handoff-only boundary. No registry seed, adapter,
    public refresh, account action, or source mutation was made in this review pass.

- **2026-07-17 — Bay Area publisher follow-on: held municipal/library sources; Oakland is a narrow factual-source
  increment.** The next public-publisher review kept the same rule: accessibility is not authority to reuse
  material.
  - **City of Berkeley:** the official [community events page](https://berkeleyca.gov/community-recreation/events)
    and robots surface are technically readable, but the current
    [Website Policy](https://berkeleyca.gov/website-policy) prohibits commercial reuse, distribution, and
    mirroring without written permission. There is no reviewed official feed/API. Hold it.
  - **Presidio:** its public WordPress
    [event API](https://wp.presidio.gov/wp-json/tribe/events/v1/events) and
    [ICS feed](https://wp.presidio.gov/events/list/?ical=1) are technically attractive, but the source has no
    explicit compatible reuse grant and exposes many third-party organizers. Its public endpoints are not an
    authorization to ingest them. Hold it pending Presidio-specific written approval; do not use the owner-keyed
    NPS API as a workaround.
  - **City of Oakland:** its official [calendar](https://www.oaklandca.gov/Event-Calendar),
    [sitemap](https://www.oaklandca.gov/sitemap.xml), and robots surface support a deliberately limited factual
    discovery/handoff posture. The public policy reviewed does not provide a broad reusable-content license, so
    this is not a claim of permission to republish promotional material; owner/legal confirmation remains advisable.
    The accepted scope is exact event facts plus source attribution/canonical handoff only—never images, full
    promotional copy, contact data, or ticket/registration traversal.
  - **Fremont and Santa Clara:** both official calendars are held. Fremont's
    [Website Use Policy](https://www.fremont.gov/government/website-use-policy) expressly prohibits commercial
    reuse/distribution/mirroring without written permission; its paginated HTML calendar has only per-event exports
    and does not fit the closed CivicEngage RSS contract. Santa Clara's legacy CivicPlus calendar has the same
    per-event-only pattern, while its newer BeWith calendar is SPA-rendered and the platform terms prohibit automated
    queries/scraping. Santa Clara's own
    [Terms of Use](https://www.santaclaraca.gov/our-city/government/governance/privacy-website-policies/terms-of-use)
    likewise prohibit commercial reuse/distribution/mirroring without permission. Both robots surfaces were
    unavailable or non-parseable in review. Do not add a workaround adapter; revisit only with written City
    permission and a reviewed feed/API.
  - **Cupertino:** hold its official Events Directory. The City's
    [Website Policy](https://www.cupertino.gov/Your-City/Transparent-Cupertino/Digital-Policies/Website-Policy)
    expressly prohibits commercial use and reuse/distribution/mirroring of City content without written
    permission. Its official directory is a short, paginated HTML listing with no published RSS, JSON, iCal, or
    API contract, stable occurrence identity, canonical-handoff rule, or durable physical/virtual field. Do not
    turn the listing into a crawler or infer Granicus internals. Revisit only with written City authorization and
    a documented, bounded machine-readable feed/API.
  - **City of Berkeley public meetings:** hold it. The City's current Council agenda surface is on
    `berkeleyca.gov` and does not link an active Legistar calendar. Both plausible public Granicus clients,
    `/v1/Berkeley/Events` and `/v1/BerkeleyCA/Events`, return the publisher's explicit unconfigured-InSite
    error; the similarly named InSite calendar pages return only `Invalid parameters!`. There is therefore no
    verified endpoint, pagination contract, record shape, canonical handoff, or source-truth location policy to
    seed. Berkeley's [Website Policy](https://berkeleyca.gov/website-policy) also prohibits commercial use,
    reuse/distribution, and mirroring of City or contracted-vendor content without written permission outside
    designated Open Data. Do not infer a Legistar host, scrape eAgenda pages, or use an undocumented workaround.
    Revisit only if the City supplies a documented active feed/API and owner/legal obtains written permission (or
    an applicable open-data licence) for the narrow factual, attributed-handoff posture.
  - **San Francisco Board and committee meetings:** hold it. The City officially links
    `sfgov.legistar.com/Calendar.aspx`, but its only verified public API client, `SFGov`, returns an explicit
    publisher configuration error for every `/v1/SFGov/Events` request. With no successful first page, the project
    cannot verify an Events schema, page/cap contract, cancellation/location semantics, or a safe source profile.
    The Calendar's observed handoffs use `MeetingDetail.aspx?ID=...&GUID=...`, not the existing civic adapter's
    `LEGID` shape, and historic official material includes blank-location remote meetings plus `REMOTE MEETING VIA
    VIDEOCONFERENCE`. Do not infer a generic Legistar profile, scrape the calendar, or activate an undocumented
    RSS endpoint (the advertised RSS control is disabled). Revisit only if City/Granicus restores its Events API,
    then separately review a closed `ID`/`GUID` handoff contract and physical/virtual fixtures. City policy
    reserves editorial/creative rights; no broader reuse is implied by calendar access.
  - **San Mateo County public meetings:** hold it. County Board and Parks pages link an official
    `sanmateocounty.legistar.com` calendar and physical/hybrid meeting records, but neither the County nor
    Granicus publishes the required `webapi.legistar.com/v1/{Client}` value. Do not derive or probe a client name
    from the host. Without a documented working Events endpoint, the project cannot verify token behavior, OData
    pagination/cap, API record fields, virtual-only semantics, or API-supplied handoffs. The public detail links
    are themselves incompatible: Board records use matching `LEGID`, while Parks records use `ID` plus `GUID`.
    A later permitted implementation needs a County-published active API client plus a separately typed,
    source-backed handoff contract and physical/virtual fixtures; do not broaden the current `LEGID` parser or
    scrape the HTML calendar. The County disclaimer supports at most the existing factual, attributed-handoff
    posture and not reuse of agendas, attachments, media, contacts, or external-material content.
  - **Contra Costa County public meetings:** hold it; this is separate from the enabled Contra Costa County
    Library RSS source. The County directs current meetings to `contra-costa.legistar.com`, but does not publish
    the required Granicus Web API `{Client}` value or authorize product API use. Do not infer or probe it. The
    Calendar's official handoffs use `MeetingDetail.aspx?ID=...&GUID=...`, not the existing typed adapter's
    `LEGID` contract, and current rows mix primary Bay Area venues with Zoom, blank, cancelled, rescheduled, and
    out-of-region locations. Per-event iCal exports are not a documented bulk feed and must not become an HTML
    crawler. Revisit only after County/Granicus documents the active client and permits bounded read-only product
    use, then implement a separate exact `ID`/`GUID` profile with primary-physical, status, pagination, and
    handoff fixtures. County/Granicus public access and absent Legistar robots rules are not a content-reuse grant.
  - **San Francisco Public Library:** do not add a direct crawler or BiblioCommons profile. Its public
    [Drupal event list](https://sfpl.org/events) is a 2,000-plus-result paginated surface with no documented
    feed/API; `robots.txt` allows the page but SFPL reserves all rights and its Internet policy supplies no reuse
    licence. The nominal BiblioCommons RSS feed is currently a valid zero-item channel, so it cannot safely cover
    SFPL. The already-enabled official
    [DataSF Our415 dataset](https://data.sfgov.org/Economy-and-Community/Our415-Events-and-Activities/8i3s-ih2a)
    is PDDL-licensed, documents SFPL as a source, and is ingested through `datasf-our415-events`; it gives a
    permissive, supported subset rather than a claim of full SFPL coverage. Hold direct ingestion unless SFPL grants
    permission or publishes a supported feed/API.
  - **Sonoma County Library:** hold it. The nominal official BiblioCommons RSS endpoint is a valid but empty
    channel, while the actual separate calendar host's `robots.txt` has an explicit catch-all `Disallow: /`.
    Its paginated HTML/feed-like endpoints are undocumented and cannot be treated as a workaround. Copyright and
    permissions pages provide no affirmative calendar-data reuse grant. Revisit only with publisher permission,
    an allowed documented feed/API, and an authoritative physical-location/cancellation contract.
  - **Marin County Free Library:** its official BiblioCommons RSS has a real, bounded, physical-branch contract and
    expressly exempts RSS/XML from the automated-harvesting ban. It has the same personal/non-commercial Service
    Content restriction as the already-reviewed Oakland, San José, and Contra Costa BiblioCommons sources. The
    project therefore retains the same discovery-only, attributed-handoff posture and carries forward the
    owner/legal review requirement before commercial redistribution; its branch-only implementation is recorded
    below.
  - **San Mateo County Libraries, all physical branches:** the official
    [`/v2/libraries/smcl/rss/events`](https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events) contract
    supports one closed, ordered physical-branch query without an account. It exposes 25 RSS items per page but no
    total/next cursor, so the reviewed 90-day pass is capped at 150 pages and fails rather than retaining a partial
    window. The exact approved IDs cover Atherton, Belmont, Brisbane, East Palo Alto, Foster City, Half Moon Bay,
    Millbrae, North Fair Oaks, Pacifica Sanchez, Pacifica Sharp Park, Portola Valley, San Carlos, and Woodside;
    `0K` Bookmobile and `0Z` Pacifica Sanchez Outpost are not approved physical-branch locations. BiblioCommons'
    RSS/XML exception and personal/non-commercial Service Content boundary are the same as Marin and the existing
    library sources. The all-branch source supersedes the redundant Millbrae-only cadence while preserving its
    durable provenance; commercial redistribution still requires owner/legal approval.
  - **Santa Clara County Library District, all physical branches:** the official
    [`/v2/libraries/sccl/rss/events`](https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events) contract
    supports an exact eight-branch OR query: Campbell, Cupertino, Gilroy, Los Altos, Milpitas, Morgan Hill,
    Saratoga, and Woodland. The public branch/event navigation distinguishes those from Bookmobile, Services &
    Support Center, online `BC_VIRTUAL`, and unreviewed/offsite locations; only the eight physical IDs are
    approved. The 90-day combined feed had 1,019 current items, 25 items per page, and no usable total/next
    cursor, so a 50-page cap gives nine pages of headroom over the observed 41-page pass and fails closed at the
    cap. SCCLD's BiblioCommons terms expressly except RSS/XML harvesting but retain the same personal/non-commercial
    Service Content boundary; use remains attributed discovery/handoff only. The all-branch source supersedes the
    redundant Milpitas and Saratoga cadences without deleting their provenance.
  - **Alameda County Library, all physical branches:** this is the next reviewed BiblioCommons candidate. Its
    official `aclibrary` gateway feed has exact physical IDs for Albany, Castro Valley, Centerville, Cherryland,
    Dublin, Fremont, Newark, Niles, San Lorenzo, and Union City. A reviewed 90-day query returned 662 rows over
    27 pages; the proposed fixed 40-page, five-second source would fail closed above that bound and supersede only
    the redundant Fremont slice. `MOS` Mobile Library, virtual, locker, offsite, and dynamic locations stay outside
    the whitelist. Its terms have the same RSS/XML exception and non-commercial Service Content limit, so it is
    eligible only for the existing attributed handoff posture; the completed implementation is recorded below.
  - **Solano and Napa County Libraries:** both are held, not enabled sources. Solano's public Communico iCal
    export is technically usable but is not an affirmative content-reuse grant: its footer reserves all rights and
    its published terms are a room-reservation policy, while the `robots.txt` allowance and export surface do not
    authorize automated collection or commercial redistribution. The review verified the exact same-origin
    `/feeds?data=...` iCal contract, UTC `VEVENT` timing/status/GEO, and the canonical numeric-UID handoff shape
    `https://solanolibrary.communico.co/event/{uid}` without fetching it during a refresh. It also proved that the
    provider ignores date filters and caps a feed at 500 events, so any future approved implementation would need
    one fixed, paced request per branch, local 90-day clipping, and whole-run failure at a per-branch `>=500` cap;
    never use its structurally incomplete RSS feed or an undocumented API/front-end workaround. More importantly,
    the nominal Law Library feed (`3494`) currently emits `LOCATION: -`, so its venue/city cannot be inferred from
    the branch filter; it must remain excluded rather than be described as an all-physical-branches source. Only a
    later owner/legal approval of the minimal factual, attributed-handoff posture could reopen a strictly bounded
    nine-verified-branch design. Napa is held: its installed iCal location filter leaked other branches and virtual
    events, approached the vendor cap, and its official copyright posture provides no affirmative reuse grant. Do
    not work around either boundary with undocumented JSON or front-end endpoints.

- **2026-07-17 — Bay Area catalog increment COMPLETE: City of Oakland Event Calendar.** Migration `0068` adds the
  reviewed `oakland-city-events` registry record and the `oakland_html` catalog mode. Its `1,440`-minute cadence,
  five-second same-host interval, and `160` total-GET budget are fixed in both registry and closed adapter profile
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Complete, bounded retrieval:** `OaklandCatalogFetcher` requests only the exact namespaced public sitemap and
    approved same-origin `/Event-Calendar/` detail URLs. It does not automate the stateful WebForms list pager.
    A duplicate, malformed, unapproved, redirected, oversized, empty, or `>=160` detail set fails before any
    partial catalog write. Every sitemap/detail request shares the five-second per-host floor, and all redirects,
    query/fragment paths, userinfo/port lookalikes, external canonicals, XML entity declarations, and endpoint drift
    fail closed.
  - **Minimal parsing and honest fields:** the adapter reads only the title, machine-readable occurrence attributes,
    one physical Bay Area map marker, exact canonical handoff, compact categories, and a cost label. It accepts
    explicit city text from the source address (so a Berkeley location remains Berkeley rather than being guessed as
    Oakland), accepts no city guess when absent, and rejects virtual/out-of-region/ambiguous-DST data. An exact
    `Free` label is `FREE`; every other/missing cost remains `UNKNOWN`, so paid discovery remains included without
    falsely certifying a price. It retains no description, image, contact, or registration URL, does not infer
    cancellation from absence/body text, and makes no external follow-up request.
  - **Lease safety:** the catalog-refresh claim now derives a source-sized lease from the reviewed request cap and
    pace, with final-write buffer. Oakland's maximum valid sitemap-plus-detail pass therefore reserves `860` seconds
    rather than silently losing the former five-minute claim during a compliant slow crawl. A post-Pacer registry
    edit that exceeds the already claimed lease releases the run before source egress; an impossible one-hour budget
    makes zero Pacer, source, or catalog calls.
  - **Evidence:** fixture coverage proves factual parsing, exact canonical handoff, Berkeley city preservation,
    external-link non-traversal, cost/category limits, pacing, cap/redirect/XML/empty-sitemap failure, and registry
    tamper rejection. Catalog-refresh tests cover the long bounded lease, impossible budget, and post-claim
    pacing-cap race. PostgreSQL source-registry integration passed after applying `0068` as migration owner and
    reading the row as `ec_app` through its capability boundary.
  - **Explicit boundary:** no live Oakland refresh, recurrence scheduler, browser automation, WebForms POST,
    account, credential, provider mutation, payment, external registration, or commit was made. The adapter is
    ready for an authorized manual catalog refresh; recurring production cadence remains a separate owner decision.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**397 unit, 142 ordinary integration; 187 source files**) against local Docker. Focused
    Oakland adapter/catalog-refresh and real PostgreSQL registry suites also pass.

- **2026-07-17 — Bay Area catalog increment COMPLETE: Marin County Free Library branch events.** Migration `0069`
  adds `marin-county-free-library-events` under the existing closed `bibliocommons_rss` adapter. It is deliberately
  a library-branch source, not a claim to cover every physical event in Marin County (FR-3.1/FR-3.7/FR-10.3/10.4,
  NFR-8).
  - **Closed source contract:** the only request surface is the anonymous official gateway RSS feed, pinned to the
    ordered physical branch IDs `MB, MC, MM, MF, MI, MA, MN, MP, MH, MS` (Bolinas, Civic Center, Corte Madera,
    Fairfax, Inverness, Marin City, Novato, Point Reyes, South Novato, and Stinson Beach). The adapter adds its
    existing Pacific 90-day `startDate`/`endDate` window and numbered pages, never trusts feed pagination, does not
    follow the BiblioCommons handoff, event detail, registration, image, or external URL, and fails if all 25
    reviewed pages are full rather than publishing a partial window.
  - **Truth and pacing:** candidates require a reviewed physical branch location, matching GUID/link event identity,
    valid UTC occurrence timing, and the existing cancellation/virtual/hybrid guards. Recurrences retain the
    source event ID plus start timestamp; offsite/dynamic location IDs and the explicit virtual facet are excluded.
    There is no structured price, so every price stays `UNKNOWN`. Marin's source profile locks its five-second
    gateway cadence in addition to query order and cap; lowering it is rejected before I/O. Its bounded 25-page
    pass fits the catalog refresh's source-sized lease.
  - **Rights boundary:** Marin's terms expressly permit RSS/XML collection and encourage a link back to the original
    extract, which the handoff supplies. They still limit Service Content to personal/non-commercial use absent a
    separate agreement. This is the established BiblioCommons discovery-only pattern, not permission to commercially
    redistribute source content; owner/legal review remains required before that use.
  - **Evidence:** hermetic fixtures cover exact ordered query/window construction, branch and hybrid retention,
    virtual/cancelled/offsite/mismatched/out-of-window exclusion, recurring identity, unknown pricing, five-second
    pacing, profile tampering, and 25-page cap failure. PostgreSQL coverage reads the seeded row through the app
    role after an explicit `0068` downgrade / corrected `0069` upgrade.
  - **Explicit boundary:** no live Marin RSS refresh, account, credential, sign-in, registration, payment, source
    mutation, scheduler, or commit was made. A recurring production cadence and commercial redistribution remain
    separate owner/legal decisions.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**401 unit, 142 ordinary integration; 187 source files**) against local Docker. No new
    concrete composition dependency was needed.

- **2026-07-17 — Bay Area catalog increment COMPLETE: San Mateo County Libraries all physical branch events.**
  Migration `0070` adds the separately reviewed `smcl-all-physical-branches-events` source; `0071` disables the
  now-redundant `smcl-millbrae-events` refresh seed without deleting its source row, observations, or refresh-run
  provenance (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Closed source contract:** the only request surface is the anonymous official BiblioCommons gateway RSS feed,
    pinned to the ordered physical IDs `1A, 1B, 1R, 1E, 1F, 1H, 1M, 1N, 1Z, 1P, 1V, 1S, 1W` and to matching
    `https://smcl.bibliocommons.com/events/...` GUID/link handoffs. It covers Atherton, Belmont, Brisbane, East
    Palo Alto, Foster City, Half Moon Bay, Millbrae, North Fair Oaks, Pacifica Sanchez, Pacifica Sharp Park,
    Portola Valley, San Carlos, and Woodside. It deliberately excludes the `0K` Bookmobile, the unreviewed `0Z`
    outpost, virtual-only events, and every dynamic/offsite location; `1Z` is the reviewed Sanchez branch and is
    distinct from excluded `0Z`.
  - **Bounded, truthful retrieval:** the adapter adds its existing Pacific 90-day request window and walks only
    numbered pages. The feed provides neither a total nor a next cursor, so a short page completes the refresh and
    a full 150-page cap fails closed. Candidates require a reviewed physical location, matching GUID/link identity,
    valid UTC occurrence timing, and existing cancellation/virtual/hybrid guards. Recurrences retain their source
    event ID plus start timestamp. There is no structured price, so all price values remain `UNKNOWN`; no event
    detail, ticket, image, registration, or external URL is fetched.
  - **Pacing and duplication control:** the registry locks a five-second source interval and the 150-page cap gives
    the catalog worker an 810-second source-sized lease. The shared BiblioCommons adapter now honors the stricter
    of the preceding and current same-host intervals, so a prior five-second SMCL read cannot be followed by a
    legacy 1.5-second gateway read too soon. Since `1M` is included in the new query, `0071` disables the legacy
    Millbrae-only source rather than double-fetching it; its durable history remains intact. Because `0071` does
    not record an operator's prior enabled flag, its downgrade remains fail closed and reactivation requires an
    explicit owner/operator review.
  - **Rights boundary:** the source remains an attributed, discovery-only handoff. Its RSS/XML exception permits
    this bounded collection, but BiblioCommons Service Content is still personal/non-commercial absent a separate
    agreement. This is not authorization to commercially redistribute source content; owner/legal review remains
    required before that use.
  - **Evidence:** hermetic fixtures lock query order/window, physical and hybrid retention, `0K` Bookmobile,
    virtual/cancelled/boundary/GUID-link mismatch exclusion, recurrence identity, unknown price, five-second
    pacing, cross-profile same-gateway pacing, pre-I/O tamper rejection, and the 150-page failure path. PostgreSQL
    tests lock the exact registry row and source activation state; hermetic migration coverage proves `0071`
    downgrade cannot emit a re-enable update for the preserved Millbrae seed.
  - **Explicit boundary:** no live SMCL request, account, credential, sign-in, registration, payment, source
    mutation, scheduler, or commit was made. A recurring production cadence and commercial redistribution remain
    separate owner/legal decisions.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**406 unit, 142 ordinary integration; 187 source files**) against local Docker. Focused
    BiblioCommons coverage, the 0070/0071 migration round-trip, and app-role registry assertions also pass.

- **2026-07-17 — Bay Area catalog increment COMPLETE: Santa Clara County Library District all physical branch
  events.** Migration `0072` adds `sccld-all-physical-branches-events`; `0073` retires the now-redundant
  Milpitas-only and Saratoga-only refresh seeds while retaining their source rows, observations, and refresh-run
  provenance (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Closed source contract:** the only request surface is the anonymous official BiblioCommons gateway RSS feed,
    pinned to ordered IDs `CA, CU, GI, LA, MI, MH, SA, WO` and matching
    `https://sccl.bibliocommons.com/events/...` GUID/link handoffs. They are Campbell, Cupertino, Gilroy, Los
    Altos, Milpitas, Morgan Hill, Saratoga, and Woodland. Bookmobile, Services & Support Center, online
    `BC_VIRTUAL`, and every unknown/offsite/dynamic location are excluded by the static physical whitelist.
  - **Bounded, truthful retrieval:** the adapter adds a Pacific 90-day request window and accepts only a reviewed
    physical location, matching GUID/link identity, valid UTC occurrence timing, and the existing
    cancellation/virtual/hybrid guards. It keeps recurrence identity as event ID plus start timestamp and leaves
    price `UNKNOWN`; it never follows a handoff, registration, contact, image, ticket, or external URL. The feed
    has 25 items per page and no total or next cursor, so a short page completes the pass and a full 50-page cap
    fails closed rather than publishing partial coverage. The bounded pass reserves a 310-second refresh lease at
    the five-second gateway cadence.
  - **Supersession safety:** `MI` and `SA` are included in the comprehensive source, so `0073` disables only the
    two overlapping legacy seeds instead of double-fetching them. Upgrade preserves every durable legacy record.
    Neither `0071` nor `0073` records a prior operator-controlled enabled flag; their downgrades intentionally do
    not re-enable a source, so any reactivation remains an explicit owner/operator review rather than surprise
    network egress.
  - **Rights boundary:** SCCLD/BiblioCommons terms expressly except RSS/XML collection from the automated-harvesting
    prohibition, but Service Content remains personal/non-commercial absent a separate agreement. This remains a
    factual, attributed discovery handoff, not authorization to commercially redistribute source content; owner/
    legal review is required before that use.
  - **Evidence:** hermetic fixtures lock query/order/window, physical and hybrid retention, virtual/cancelled/
    boundary/GUID-link mismatch/offsite exclusion, recurrence identity, unknown pricing, five-second pacing,
    same-gateway high-to-low pace inheritance, profile tampering before I/O, and the 50-page failure path. Registry
    integration locks the source row and retired-source activation state. Hermetic migration coverage proves both
    disable-only downgrades emit no SQL that could silently re-enable an operator-disabled source.
  - **Explicit boundary:** no live SCCLD source refresh, account, credential, sign-in, registration, payment,
    source mutation, scheduler, or commit was made. Recurring production cadence and commercial redistribution
    remain separate owner/legal decisions.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**413 unit, 142 ordinary integration; 187 source files**) against local Docker. Focused
    SCCLD fixtures, owner-run migration checks, app-role registry assertions, and the fail-closed downgrade
    regression all pass.

- **2026-07-17 — Bay Area catalog increment COMPLETE: Alameda County Library all physical branch events.**
  Migration `0074` adds `alameda-county-library-all-physical-branches-events`; `0075` retires the redundant
  Fremont-only refresh seed while retaining its source row, observations, and refresh-run provenance
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Closed source contract:** the only request surface is the anonymous official BiblioCommons gateway RSS feed,
    pinned to ordered IDs `ALB, CSV, CTV, CHY, DUB, FRM, NWK, NLS, SLZ, UCY` and matching
    `https://aclibrary.bibliocommons.com/events/...` GUID/link handoffs. They cover Albany, Castro Valley,
    Centerville, Cherryland, Dublin, Fremont, Newark, Niles, San Lorenzo, and Union City. `MOS` Mobile Library,
    virtual, locker, offsite, and dynamic locations remain outside the static physical whitelist; Niles remains a
    reviewed branch even when a particular 90-day window is empty.
  - **Bounded, truthful retrieval:** the adapter adds a Pacific 90-day request window and accepts only reviewed
    physical locations, matching GUID/link identity, valid UTC occurrence timing, and the existing
    cancellation/virtual/hybrid guards. It retains recurrence identity as event ID plus start timestamp, retains
    structured source venue/city/coordinates, leaves price `UNKNOWN`, and never fetches a handoff, registration,
    contact, image, ticket, or external URL. With 25 RSS items per page and no total/next cursor, a short page
    completes the pass and a full 40-page cap fails closed rather than publishing partial coverage. The five-second
    cadence reserves a 260-second source-sized refresh lease.
  - **Supersession safety:** `FRM` is included in the comprehensive source, so `0075` disables only the overlapping
    legacy Fremont seed rather than double-fetching it. Durable history remains intact. As with the preceding
    retirements, the migration does not record an operator's former enabled flag, so downgrade emits no automatic
    re-enable; reactivation requires explicit owner/operator review.
  - **Rights boundary:** ACL/BiblioCommons terms expressly except RSS/XML collection from the automated-harvesting
    prohibition and require a link back for displayed extracts, but Service Content remains personal/non-commercial
    absent a separate agreement. This remains a factual, attributed discovery handoff, not permission to
    commercially redistribute source content; owner/legal review is required before that use.
  - **Evidence:** hermetic fixtures lock query/order/window, physical and hybrid retention, actual `MOS` Mobile
    Library/virtual/cancelled/boundary/GUID-link mismatch/offsite exclusion, recurrence identity, unknown pricing,
    five-second pacing, inherited same-gateway pace, pre-I/O profile tampering, and the 40-page failure path.
    Registry integration locks the source row and retired Fremont activation state; the migration regression suite
    now covers all three fail-closed source-retirement downgrades.
  - **Explicit boundary:** no live ACL source refresh, account, credential, sign-in, registration, payment, source
    mutation, scheduler, or commit was made. Recurring production cadence and commercial redistribution remain
    separate owner/legal decisions.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**419 unit, 142 ordinary integration; 187 source files**) against local Docker. Focused ACL
    fixtures, owner-run migration checks, app-role registry assertions, and all three fail-closed retirement
    downgrade regressions also pass.

- **2026-07-17 — Bay Area source review / catalog increment COMPLETE: City of Alameda public meetings.**
  Migration `0076` adds the separately typed `alameda_legistar` source
  `alameda-legistar-meetings`, reusing the closed civic `LegistarCatalogFetcher` only through the explicit
  City of Alameda profile. It is factual discovery with attribution and a canonical human handoff only
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8); no registration, account, credential, payment, calendar, or source
  mutation capability is introduced.
  - **Review / rights boundary:** the City's official
    [Agendas, Minutes & Announcements](https://www.alamedaca.gov/GOVERNMENT/Agendas-Minutes-Announcements)
    page identifies `alameda.legistar.com` as the online database for upcoming public agendas. Granicus'
    public [Legistar API examples](https://webapi.legistar.com/Home/Examples) document anonymous
    `GET /v1/{Client}/Events`, OData filtering, and pagination for public/InSite-viewable records. The reviewed
    client is exactly `Alameda`—not `AlamedaCA`. Neither the API nor handoff host published a `robots.txt`
    disallow during review. The City pages establish public official access but not an affirmative commercial
    redistribution license; this source is permitted only under the established minimal factual,
    attributed-handoff posture. Catalog storage must exclude images, agendas, attachments, documents,
    promotional copy, contacts, and source-derived URLs beyond the matching detail handoff. Owner/legal approval
    remains required before broader commercial redistribution.
  - **Closed request and handoff contract:** the only worker request is
    `https://webapi.legistar.com/v1/Alameda/Events`, with the adapter-owned local 90-day OData filter,
    `EventDate asc,EventId asc` ordering, `$top=100`, and offset pages. The registry admits only that API
    origin, retains the 360-minute cadence, 1.5-second per-host floor, and five-page / 500-row cap; a full fifth
    page, duplicate event id, redirect, malformed page, or endpoint escape fails the whole refresh instead of
    publishing a partial window. Unsafe individual records are excluded. The only handoff accepted by the adapter
    is a matching `https://alameda.legistar.com/MeetingDetail.aspx?LEGID=<EventId>` URL, normalized to the
    reviewed host and never fetched. `Hidden`, cancelled, closed-session, virtual-only, malformed, past,
    blank-venue, and unsafe
    handoff records are excluded; physical and explicitly hybrid venues retain only source-supplied title, local
    start, venue, and compact comment. Price, city, coordinates, and end time remain unknown when Legistar does
    not supply them.
  - **Evidence / implementation:** an API-shaped local fixture locks the exact Alameda path, OData query,
    canonical handoff, physical/hybrid retention, and hidden/cancelled/closed/virtual/past/malformed/unsafe
    rejection. It also proves the closed profile refuses a changed source key, client path, query, mode, or
    handoff-only flag before any request. Registry integration locks the exact seed, sole API origin, mode,
    five-page cap, 360-minute cadence, and 1.5-second floor; composition maps only this mode to the existing
    Legistar fetcher. No live refresh, account action, registration, payment, handoff fetch, or source mutation
    was made by this implementation increment.
  - **Focused verification:** the Alameda/San José/Sunnyvale Legistar unit set passed (10 tests), the local
    owner migration applied, and the app-role catalog-registry integration test passed before the subsequent
    safety-containment migration.

- **2026-07-17 — Bay Area catalog safety hold: UC Berkeley Events.** Migration `0077` disables the existing
  `berkeley-events` LiveWhale registry record while preserving its source row, refresh-run history, and
  observations. The migration's downgrade intentionally emits no re-enable SQL: a later owner-reviewed change is
  required before any Berkeley refresh can resume (FR-10.3/NFR-8).
  - **Verified hold reasons:** Berkeley's official public calendar and personal iCal/RSS export surface do not
    provide an affirmative automated/commercial reuse grant; the reviewed feed can contain physical events outside
    the Bay Area and rows without authoritative physical-location evidence; and the existing three-page LiveWhale
    limit stops after its cap rather than failing when the publisher declares additional pages. None of those facts
    is remedied by the descriptive `bay_area_9_county` registry label.
  - **Reactivation boundary:** a future owner/legal review must authorize the narrow factual, attributed-handoff
    posture and separately approve a closed Berkeley adapter correction: source-proven physical/geographic
    filtering without city inference, same-origin handoff validation, and declared-page cap failure before any
    partial catalog write. The current hold is egress containment only; it does not claim to permanently retract
    existing Berkeley-derived links or canonicals. Any such data remediation must be lifecycle-aware and separately
    reviewed.
  - **Combined verification:** `make install && make up && make migrate && make test && make test-integration &&
    make lint typecheck` passes after `0076` and `0077` (**422 unit tests; local integration suite; 187 source
    files**). The focused fail-closed downgrade regression also passes. No live source request, handoff fetch,
    provider credential, account action, payment, or commit was made.

- **2026-07-17 — Bay Area catalog safety hold: Santa Clara University and SMCCD Events.** Migration `0078`
  disables only the existing `scu-events` and `smccd-events` LiveWhale registry rows. It preserves source rows,
  refresh-run history, observations, and canonical attachments; the downgrade deliberately emits no re-enable
  SQL, so a later owner-reviewed migration is required before either worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Verified source-truth containment:** both current configured JSON endpoints remain public canonical 200
    responses, but their declared pagination exceeds the reviewed three-page cap. SCU reports 1,110 rows at
    100 per page / 12 pages; its full third page advertises page 4. SMCCD reports 484 rows at 100 per page /
    5 pages; its full third page likewise advertises page 4. The current generic adapter stops after the registry
    cap without treating a declared next page as a whole-refresh failure, so retaining either source would publish
    a known partial subset. Raising the cap alone is not an approved remedy.
  - **Physical and handoff truth:** neither feed supplies authoritative physical Bay Area evidence for every
    retained record. Across the currently reviewed SCU three-page subset, 88 of 300 rows lack usable venue/geo
    evidence, at least 70 contain `Zoom`, `Online`, `Remote`, `Virtual`, or `Webinar` text without
    `online_type=Online only` or an online flag, and event `455478` supplies San Diego coordinates
    (`32.710054,-117.156924`) with no online flag. The current flag-only guard can retain all of those records.
    SMCCD likewise has blank-location rows and unflagged `Online` records. Its current API page also emits human
    handoffs on `events.collegeofsanmateo.edu`, `events.skylinecollege.edu`, and
    `events.canadacollege.edu` in addition to `events.smccd.edu`; the current adapter accepts arbitrary HTTP(S)
    URLs instead of an explicit canonical host/path/identity contract. SCU currently emits same-host handoffs,
    but several do not match their source event ID grammar and the adapter does not enforce that either.
  - **Robots and rights boundary:** SMCCD's published `robots.txt` allows ordinary calendar paths apart from the
    `?replytocom` rule, but robots is not a content-reuse grant. Its public calendar accepts community-submitted
    events and flyers, and no affirmative event-content reuse license was identified. SCU's `robots.txt` currently
    returns a CloudFront 403 rather than a usable authorization, and its official copyright guidance treats online
    materials as protected absent permission, license, or a valid exception. Neither source is therefore approved
    for continued automated factual collection or commercial redistribution merely because the APIs are public.
  - **Reactivation boundary:** owner/legal approval must first authorize the narrow factual,
    attributed-canonical-handoff posture. A separately reviewed closed adapter correction must then require a
    source-provided physical venue plus source-published in-region city/coordinates or an independently reviewed
    fixed campus mapping (never publisher-city inference), reject remote text as well as flags, validate declared-
    page completion before any catalog write, and accept only exact HTTPS canonical handoffs. SCU needs a same-host
    source-ID path grammar; SMCCD needs an explicit owner-reviewed decision on its official
    multi-campus handoff hosts and matching path grammar. Any cleanup of already retained links/canonicals is a
    separate lifecycle-aware decision. No LiveWhale fetcher, registry cap, existing observation, or canonical data
    was changed by this containment increment; no source request, handoff fetch, account action, payment, or commit
    was made after the read-only audit.
  - **Verification:** local migration-owner application of `0078`, the app-role registry assertion, and the
    five-source fail-closed downgrade regression passed. `make install && make up && make migrate && make test &&
    make test-integration && make lint typecheck` passes (**423 unit tests; local integration suite; 187 source
    files**), with focused format checks clean. No source refresh was run.

- **2026-07-17 — Bay Area catalog increment COMPLETE: City of Oakland public meetings.** Migration `0079`
  adds the separately typed `oakland_legistar` source `oakland-legistar-meetings`, reusing the closed civic
  `LegistarCatalogFetcher` only through the exact City of Oakland profile (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  This is distinct from and does not alter the separate `oakland-city-events` / `oakland_html` adapter.
  - **Official / rights boundary:** the City of Oakland's
    [City Council Meeting Information](https://www.oaklandca.gov/Government/Meetings-Agendas/City-Council-Meeting-Information)
    page links its meeting schedule directly to `oakland.legistar.com`; its
    [Open Meetings](https://www.oaklandca.gov/Government/Boards-Commissions/Public-Ethics-Commission/Open-Government/Open-Meetings)
    guidance identifies Granicus as the City's agenda-management platform and describes public agenda
    accessibility/reuse requirements. Granicus'
    [Legistar API examples](https://webapi.legistar.com/Home/Examples) document anonymous public/InSite `GET`
    event lists, OData filtering, and paging. Those facts support only the established factual,
    attributed-handoff posture—not a blanket commercial license. The catalog retains no agendas, attachments,
    minutes, video, images, contacts, or source-derived URLs beyond the matching human meeting-detail handoff;
    owner/legal review remains required before broader commercial redistribution.
  - **Closed request / handoff contract:** the only worker request is exactly
    `https://webapi.legistar.com/v1/Oakland/Events` (client `Oakland`, never `OaklandCA`) with the adapter-owned
    local 90-day predicate, `EventDate asc,EventId asc` ordering, `$top=100`, and offset pages. The registry
    permits only `https://webapi.legistar.com`, keeps the 360-minute cadence, 1.5-second per-host floor, and
    five-page cap; a full fifth page, duplicate event id, redirect, malformed page, or endpoint escape fails the
    whole refresh rather than retaining a partial window. Unsafe individual records, including mismatched
    handoffs, are excluded. The sole accepted user handoff is a matching
    `https://oakland.legistar.com/MeetingDetail.aspx?LEGID=<EventId>` record (the publisher's `GID`/`G` query
    parameters may remain); it is normalized and never fetched.
  - **Truthful physical scope:** the source describes the City Calendar's public returned civic meetings; it does
    not claim to cover every Oakland public meeting. `EventId` plus source-local start forms the source identity;
    title, start, venue, compact comment, and canonical handoff are retained. Price remains `unknown`, and city,
    coordinates, address, and end time are never inferred. Explicitly cancelled, hidden, closed-session,
    blank/malformed, past, unsafe-handoff, and virtual-only rows are excluded. The shared anchored virtual-only
    guard now recognizes `Teleconference`, `Tele-Conference`, `Tele - Conference`, and `Tele- Conference`, based
    on reviewed Oakland public records; a hybrid label beginning with a physical venue remains eligible.
  - **Evidence / implementation:** an API-shaped local fixture locks the exact Oakland path, query, uppercase
    source-host canonical handoff with matching `LEGID` and additional `GID`/`G` parameters, source physical and
    physical-first hybrid retention, each historic virtual-only spelling, and the cancellation/hidden/closed/
    blank/malformed/past/unsafe/mismatched-handoff guards. It also proves profile key, endpoint, query, mode, and
    handoff-only tampering make zero requests. Registry integration locks the source key, sole API origin, mode,
    cap, cadence, and pacing; composition maps only the new mode to the existing Legistar fetcher.
  - **Verification:** the local owner migration reaches `0079 (head)`; focused Oakland/San José/Sunnyvale/Alameda
    Legistar units pass (12), and the app-role catalog-source registry integration passes (5). `make install &&
    make up && make migrate && make test && make test-integration && make lint typecheck` passes (**425 unit
    tests; local integration suite; 187 source files**); focused Ruff-format checks are clean.
  - **Explicit boundary:** no live Oakland source refresh, meeting-detail fetch, account action, credential,
    sign-in, registration, payment, calendar mutation, or commit was made by this increment.

- **2026-07-17 — Bay Area catalog safety hold: Stanford, SJSU, and UCSF Localist calendars.** Migration
  `0080` disables only `stanford-events`, `sjsu-events`, and `ucsf-events`. It preserves their source rows,
  refresh-run history, observations, and canonical attachments; its downgrade deliberately emits no re-enable SQL,
  so a later owner-reviewed migration is required before any Localist worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Contractual hold, not a parser verdict:** all three rows use Localist's generic `/api/2/events` endpoint.
    Localist's [API terms](https://developer.localist.com/doc/api) require an active Enterprise Licensee or an
    applicable developer/API agreement to access or extract API data; technical anonymous-read support is not an
    authorization waiver. The project has no recorded evidence of a covered agreement. This containment therefore
    does not claim that the currently reviewed APIs are unavailable or malformed: their fixed canonical endpoints,
    explicit 20-page caps, and current in-person/hybrid filtering remain technical evidence only, not permission to
    persist publisher data. UCSF's [site terms](https://www.ucsf.edu/terms-of-use) and Stanford's
    [site terms](https://www.stanford.edu/terms) independently reinforce that a product-use posture needs written
    authorization; SJSU's [calendar criteria](https://www.sjsu.edu/eventcalendar/criteria.php) establish
    submission review, not a data-reuse grant.
  - **Reactivation boundary:** the owner must first provide written authorization that covers this product's
    factual, attributed-handoff collection and persistence, or approve a separately reviewed permitted official
    feed. A later technical review must also close the nine-county-versus-rectangle scope gap, all-day timestamp
    handling, handoff canonicalization/identity validation, declared-page/date validation, and cross-worker
    per-host pacing. Those corrections are deliberately not bundled into the containment migration.
  - **Evidence / verification:** the registry integration locks all three held rows to `enabled=false`, and the
    shared fail-closed downgrade regression covers `0080`. `make install && make up && make migrate && make test
    && make test-integration && make lint typecheck` passes (**426 unit tests; local integration suite; 187 source
    files**). No Localist adapter, page cap, credential, source content, existing observation, or canonical data is
    changed by this hold; no source refresh is run by this containment increment.

- **2026-07-17 — Bay Area catalog safety hold: Oakland Museum of California and Gardens of Golden Gate Park
  Tribe calendars.** Migration `0081` disables only `omca-events` and
  `gardens-golden-gate-park-events`. It preserves their source rows, refresh-run history, observations, and
  canonical attachments; its downgrade deliberately emits no re-enable SQL, so a later owner-reviewed migration
  is required before either Tribe worker can resume egress (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **OMCA authorization hold:** OMCA's published terms prohibit the product's storage/display of its event
    content absent prior written authorization. Its public event-list endpoint and permissive `robots.txt` are
    technical access evidence, not permission to persist its title, time, venue, price, or bounded excerpt. The
    project holds no written authorization covering automated retrieval, normalization, retention, and intended
    product display.
  - **Gardens source-truth and reuse hold:** the reviewed public event-list endpoint currently declares a complete
    one-page result and its `robots.txt` permits crawling, but robots is not a content-reuse grant. The only linked
    public terms/privacy page found during review is donor-privacy focused; the project holds no affirmative
    authorization for automated event-content reuse. More importantly, most otherwise eligible current records
    provide only a named `SF Botanical Garden` venue with no source city, state, country, or coordinates. The shared
    Tribe adapter accepts a named venue plus non-virtual flags, so it cannot fail closed if a future named physical
    venue is outside the Bay Area.
  - **Reactivation boundary:** OMCA requires written authorization covering the narrow factual,
    attributed-handoff posture, plus a separately reviewed source-proven physical/Bay-Area guard. Gardens requires
    owner/legal approval of that narrow reuse posture (or written authorization) and a separately reviewed
    source-specific location correction: accept only authoritative source location metadata or an independently
    reviewed fixed mapping for exact Gardens venues, rejecting every other named venue. Both sources also need
    regression coverage for the corrected source truth before a later owner-reviewed migration can reactivate them.
  - **Explicit boundary:** this containment changes no Tribe adapter, page cap, source data, observation, canonical
    attachment, credential, account action, registration, payment, calendar mutation, or commit.
  - **Verification:** the owner migration reaches `0081 (head)`; the app-role catalog-source registry assertion
    keeps exactly these two Tribe rows disabled while retaining every other reviewed source's expected activation
    state; and the shared fail-closed downgrade regression passes (7 tests). `make test` passes (**427 passed**),
    `make test-integration` passes (**142 passed**), and `make lint typecheck` is clean (**187 source files**).
    Focused migration Ruff-format and lint checks are also clean. No source refresh or external source call was made
    by this containment increment.

- **2026-07-17 — Bay Area CivicEngage safety correction and City of Los Altos hold.** Migration `0082` disables
  only `los-altos-events`; it preserves the source row, refresh-run history, observations, and canonical
  attachments. Its downgrade deliberately emits no re-enable SQL, so only a later owner-reviewed migration can
  resume egress (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Los Altos rights hold:** the exact official RSS contract remains technically bounded—one same-origin
    `RSSFeed.aspx?CID=All-calendar.xml&ModID=58` document, a 50-item fail-closed ceiling, paired safe
    `Calendar.aspx?EID=`/GUID handoffs, and a source-locality guard—but the City's
    [Web Policies](https://www.losaltosca.gov/449/Web-Policies) expressly prohibit unpermitted reuse,
    distribution, and commercial use of City website materials/content/information. Its `robots.txt` allowance of
    `/RSSFeed.aspx` is not a reuse grant. Reactivation requires written City authorization or a compatible licence
    for the existing narrow factual, attributed-handoff posture.
  - **Shared physical-location correction:** the closed CivicEngage adapter now rejects a location whose
    post-locality venue begins `online`, `virtual`, `zoom`, or `remote`. This prevents a local suffix from turning
    `Zoom Meeting...` or `Remote Meeting...` into false physical evidence. It deliberately does not search beyond
    the beginning of the venue: a source-published physical-first hybrid label such as `Campbell Community Center
    — Zoom available` remains eligible. It does not change San Francisco Recreation & Park's named-location
    policy or Campbell's explicit city-only policy, and it never infers hybrid semantics when the virtual marker
    begins the source label.
  - **Evidence / verification:** the CivicEngage fixture regression covers all four leading virtual labels,
    physical-first hybrid retention, and unchanged Campbell city-only retention. Registry integration asserts
    `los-altos-events` is held, and the shared no-reenable downgrade regression covers `0082`. `make install &&
    make up && make migrate && make test && make test-integration && make lint typecheck` passes (**429 unit,
    142 integration; mypy clean across 187 source files**); focused CivicEngage/migration coverage passes (19),
    `alembic heads` reports `0082 (head)`, and focused Ruff format/lint checks are clean.
  - **Explicit boundary:** no source refresh, handoff fetch, account, credential, registration, payment, calendar
    mutation, or commit was made by this increment.

- **2026-07-17 — Bay Area catalog safety hold: University of San Francisco Main Campus and Cal Performances.**
  Migration `0083` disables only `usfca-main-campus-events` and `calperformances-events`. It preserves their source
  rows, refresh-run history, observations, and canonical attachments; its downgrade deliberately emits no re-enable
  SQL, so a later owner-reviewed migration is required before either worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **USFCA source-truth and reuse hold:** the exact public Main Campus-filtered list currently has one unique
    results container, eight cards, no pager, and safe same-origin event-id handoffs. Its `Main Campus` filter is
    nevertheless not a durable physical-campus predicate: the retained list currently contains an explicit
    `Off-Campus Event - Julia Morgan Ballroom, 465 California St., SF` row. That particular row is local, but the
    adapter retains any nonempty non-virtual venue and records neither source city nor geo, so a future offsite
    named venue could enter under the `bay_area_9_county` label. The publisher's `robots.txt` permits this list,
    but its footer copyright and privacy-focused public policy provide no affirmative automated event-content reuse
    authorization; no covered authorization is recorded by the project.
  - **Cal Performances source-truth and reuse hold:** the exact public WordPress collection currently declares a
    complete 381-record/four-page sequence under the reviewed cap, and the current 90-day eligible rows use
    publisher-named Berkeley venues with safe same-origin handoffs. Its adapter, however, treats any nonempty
    non-virtual Pacific-timezone location as physical and in-region; it has no source-proven city, coordinates, or
    closed venue policy, so a future remote physical venue could enter. `robots.txt` permits the collection, but
    the published Policies/Privacy surface provides no affirmative automated collection, persistence, or
    redistribution grant; no covered authorization is recorded by the project.
  - **Reactivation boundary:** for each source, owner/legal must approve the narrow factual,
    attributed-handoff reuse posture (or provide written authorization), and a separately reviewed adapter
    correction must require source-published in-region location metadata or an independently reviewed exact
    Bay-Area venue/address mapping, rejecting every unknown or offsite venue. Regression coverage must include a
    remote physical Pacific-timezone row, unknown venue, virtual-looking named venue, and wrong same-origin handoff
    before a later owner-reviewed migration can reactivate either source. Concurrent catalog workers also require a
    shared Pacer lease rather than the present process-local pacing maps.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes against the local services and non-superuser `ec_app` application role (**430 unit tests, 142
    integration tests; Ruff clean; mypy clean across 187 source files**). `alembic heads` reports `0083 (head)`;
    the app-role registry assertion keeps exactly these two rows held, and the shared no-reenable downgrade
    regression passes (9 cases). Focused migration Ruff-format and lint checks are also clean.
  - **Explicit boundary:** this containment changes no USFCA or Cal Performances adapter, page cap, source data,
    observation, canonical attachment, credential, account action, registration, payment, calendar mutation, or
    commit.

- **2026-07-17 — Bay Area catalog safety hold: Midpeninsula Regional Open Space District and Berkeley Public
  Library.** Migration `0084` disables only `midpen-events` and `berkeley-public-library-events`. It preserves
  their source rows, refresh-run history, observations, and canonical attachments; its downgrade deliberately emits
  no re-enable SQL, so a later owner-reviewed migration is required before either worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Berkeley Public Library source-truth and reuse hold:** the live Communico feed can label a virtual event as
    `INPERSON`, while its `new_event_id` replacement relationship is currently ignored by the closed occurrence
    parser. That can retain an obsolete source identity alongside its replacement despite the otherwise bounded
    source contract. The publisher exposes no affirmative authorization for automated collection, persistence, or
    redistribution of event content; no covered authorization is recorded by the project.
  - **Midpen reuse hold:** the reviewed HTML traversal is technically bounded and its adapter accepts only the
    closed, publisher-named physical preserve list, but the public site supplies no affirmative authorization for
    automated collection, persistence, or redistribution of its event content. No covered authorization is
    recorded by the project.
  - **Reactivation boundary:** owner/legal must approve the narrow factual, attributed-handoff reuse posture (or
    provide written authorization). Berkeley Public Library additionally needs reviewed replacement lineage and a
    source-truth regression for virtual-as-`INPERSON`; both sources need a shared per-host Pacer lease with retry
    feedback before a later owner-reviewed migration can reactivate them. That work must preserve fail-closed
    bounded traversal and add regression coverage for the corrected behavior.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes against local services and the non-superuser `ec_app` application role (**431 unit tests, 142
    integration tests; Ruff clean; mypy clean across 187 source files**). `alembic heads` reports `0084 (head)`;
    the app-role registry assertion holds exactly these two sources and the shared no-reenable downgrade regression
    passes (10 cases). Focused migration Ruff-format and lint checks are also clean.
  - **Explicit boundary:** this containment changes no Midpen or Berkeley Public Library adapter, source data,
    registry seed/cap, observation, canonical attachment, credential, account action, registration, payment,
    calendar mutation, or commit.

- **2026-07-17 — Bay Area catalog safety hold: Berkeley Repertory Theatre and Yerba Buena Center for the Arts.**
  Migration `0085` disables only `berkeley-rep-shows` and `ybca-calendar`. It preserves their source rows,
  refresh-run history, observations, and canonical attachments; its downgrade deliberately emits no re-enable SQL,
  so a later owner-reviewed migration is required before either worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **Berkeley Rep source-truth and reuse hold:** the current query-free show list remains technically bounded and
    each retained card has a strict validated `tickets.berkeleyrep.org` human handoff. The adapter nevertheless
    has no closed Bay-Area physical-location predicate: a nonempty venue is sufficient, leaving future `Remote` or
    livestream labels and offsite named venues outside a fail-closed boundary. The public site supplies no
    affirmative authorization for automated collection, persistence, or redistribution of its event content; no
    covered authorization is recorded by the project. Its five-second source-local pacing floor is not a shared
    Pacer lease.
  - **YBCA source-truth and reuse hold:** the current calendar's `YBCA` venue suffix is stronger local evidence,
    but the adapter still lacks a closed rule for `Remote`/livestream labels and cannot prove stale cards are absent
    when a publisher stops listing them. The public site supplies no affirmative authorization for automated
    collection, persistence, or redistribution of its event content; no covered authorization is recorded by the
    project. Its ten-second source-local pacing floor is not a shared Pacer lease.
  - **Reactivation boundary:** owner/legal must approve the narrow factual, attributed-handoff reuse posture (or
    provide written authorization); each adapter needs a separately reviewed physical-location and stale-record
    correction with regressions for remote/livestream and removed/stale cards; and concurrent workers need a shared
    Pacer lease with retry feedback before a later owner-reviewed migration may reactivate either source.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes against local services and the non-superuser `ec_app` application role (**432 unit tests, 142
    integration tests; Ruff clean; mypy clean across 187 source files**). `alembic heads` reports `0085 (head)`;
    the app-role registry assertion holds exactly these two sources and the shared no-reenable downgrade regression
    passes (11 cases). Focused migration Ruff-format and lint checks are also clean.
  - **Explicit boundary:** this containment changes no Berkeley Rep or YBCA adapter, source data, registry seed or
    cap, observation, canonical attachment, credential, account action, registration, payment, calendar mutation,
    or commit.

- **2026-07-17 — Bay Area catalog safety hold: San Francisco Government Related Events and DataSF Our415.**
  Migration `0086` disables only `sf-gov-related-events` and `datasf-our415-events`. It preserves their source
  rows, refresh-run history, observations, and canonical attachments; its downgrade deliberately emits no re-enable
  SQL, so a later owner-reviewed migration is required before either worker can resume egress
  (FR-3.1/FR-3.7/FR-10.3/10.4, NFR-8).
  - **SF Government endpoint hold:** the currently healthy official Related Events API does not authorize automated
    egress: the publisher's `robots.txt` disallows `/`. That protocol boundary is not an authorization waiver, and
    the adapter also retains otherwise eligible rows without source-proven physical location evidence. The project
    therefore has no safe basis to refresh, normalize, or persist new source content from this endpoint.
  - **DataSF source-truth hold:** the current Our415 dataset's PDDL posture and `robots.txt` are technically
    suitable, but active rows include virtual-only, no-location, cancelled, and postponed records. The source also
    supplies broad external handoffs and stale lifecycle state that the existing adapter cannot close under the
    catalog's physical, canonical-handoff, and lifecycle requirements. Technical access and an open-data license do
    not turn those unresolved source-truth gaps into a safe automated refresh contract.
  - **Reactivation boundary:** a separately reviewed correction must fail closed on source-proven physical location,
    virtual/no-location, cancellation/postponement, stale lifecycle, and canonical same-source-or-authorized
    handoff semantics. It must also add a shared per-request Pacer lease and typed HTTP 429 retry/re-entry handling
    before a later owner-reviewed migration may reactivate either source. Any required publisher authorization for
    the SF Government endpoint remains an owner/legal prerequisite.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes against local services and the non-superuser `ec_app` application role (**433 unit tests, 142
    integration tests; Ruff clean; mypy clean across 187 source files**). `alembic heads` reports `0086 (head)`;
    the app-role registry assertion holds exactly these two sources and the shared no-reenable downgrade regression
    passes (12 cases). Focused migration Ruff-format and lint checks are also clean.
  - **Explicit boundary:** this containment changes no adapter, registry seed or cap, source data, observation,
    canonical attachment, credential, account action, registration, payment, calendar mutation, or commit.

- **2026-07-17 — P5 quality-harness synchronization hardening.** The synthetic member-Meetup fixture now exposes a
  one-shot in-process RSVP-effect signal, set immediately before its deliberate lost-ACK exception. The G1 harness
  awaits that exact seam with a bounded ten-second diagnostic timeout instead of polling a counter for one wall-clock
  second, then still asserts exactly one source effect (P5, NFR-8, ADR-003). This is test-control plumbing only:
  it changes no product workflow, registration policy, source integration, credential, calendar, or live-provider
  behavior. A focused mock unit test locks the required effect-before-ACK-loss ordering. **Focused verification:**
  `tests/unit/test_mocks.py` passes 4 tests; the exact G1 matrix passes 10 consecutive isolated runs against local
  PostgreSQL and Temporal; Ruff and strict mypy are clean across 187 source files.

- **2026-07-17 — P15a COMPLETE: durable, shared-Pacer execution for LibCal's one closed catalog GET.** The first
  catalog egress slice is intentionally restricted to the reviewed
  `mountain-view-library-events` LibCal document: it has one exact no-redirect request, no page cursor, and no
  event-detail follow-up. `CatalogRefreshWorkflow` carries only the stable source/run identity and, on a Pacer
  projection, releases the database claim, sleeps in Temporal, and reclaims that exact run rather than holding an
  activity worker (FR-10.3/10.4, NFR-8, ADR-003/005).
  - **Admission and source-error contract:** `CATALOG_HTTP_GET` is a burst-one shared Pacer bucket whose rate is
    derived from the owner-reviewed `min_interval_ms`; P15a workers require Redis-backed Pacer state before any
    source egress. LibCal no longer owns a process-local sleep. Its sole request normalizes a valid numeric or HTTP
    date `Retry-After` 429 to the typed source throttle and a 403 to the typed quarantine signal; headerless or
    malformed 429s remain ordinary retryable fetch errors rather than inventing a delay. The durable result keeps
    the provider's explicit retry/reset floor even when Redis backoff feedback is unavailable, so a transient Pacer
    outage cannot shorten an acknowledged provider wait.
  - **Routing and race fences:** the default direct `CatalogRefreshService` path refuses this one-GET mode; the
    dedicated workflow activity supplies its explicit P15a fence. The mode-aware catalog router sends it to
    `TemporalCatalogRefreshStarter` and reports `queued`; absent Redis/Temporal wiring safely reports `skipped` and
    never falls back to a direct request. Both manual and cadence workers use that router. `make workflow-worker`
    serves the Temporal activities with Redis pacing; `make catalog-refresh` and `make catalog-cadence` configure
    Redis/Temporal and queue work without waiting through a Pacer timer. The activity rechecks owner registry and
    discovery policy after admission; a changed LibCal URL/origin/mode/page/interval contract releases the run and
    obtains a fresh token before any GET.
  - **Evidence:** fixture tests cover exact one-GET behavior, numeric/date 429, malformed/headerless 429, 403,
    direct-path refusal, registry flips during Pacer admission, stale-contract release, source-run routing, and no
    fallback when the starter is absent. Temporal time-skipping coverage proves a deferred activity re-enters the
    same source/run; Redis coverage proves one shared catalog token at the reviewed interval. Full verification is
    green: `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    (**449 unit tests; 145 ordinary integration tests; Ruff clean; strict mypy clean across 189 source files**),
    plus `make quality` and `make quality-load`.
  - **Explicit boundary:** no external catalog refresh, source activation, credential/account provision, provider
    mutation, payment, calendar write, or commit was made. P15b/P15c's fixture-first multi-GET implementation is
    now restricted to San Jose and Sunnyvale Legistar; every other paginated or redirecting adapter still needs its
    own guarded durable cursor/staging/promotion contract before claiming this per-request execution path.

- **2026-07-18 — P15b COMPLETE: durable per-page San Jose Legistar refresh.** Migrations `0087`/`0088` add a
  source-contract revision, a capability-only P15b progress ledger, page facts, normalized staged candidates, and
  remote event-id guards. The non-superuser `ec_app` role has no direct access to any of those tables. It may only
  call fixed-search-path `SECURITY DEFINER` functions to prepare/reclaim a current lease, stage exactly the next
  page, pause a nonterminal run, abort a rejected stage, read a terminal normalized stage, and atomically complete a
  promoted run. The legacy completion function explicitly refuses any run with P15b progress, preventing a generic
  one-shot caller from bypassing terminal-stage/provenance verification (FR-3.8, FR-10.3/10.4, NFR-1/NFR-8,
  ADR-001/003/005).
  - **Closed request contract:** only `san-jose-legistar-meetings` / `SAN_JOSE_LEGISTAR` enters the P15b router. Its
    immutable activity plan freezes the America/Los_Angeles 90-day window and a reviewed cap of at most five exact
    OData pages (`$top=100`, deterministic order/offset). Every page uses `CATALOG_HTTP_GET` under the shared
    `catalog-origin:webapi.legistar.com` burst-one Redis bucket; page queue keys are stable
    `{run_key}:get:{page}` identities. The adapter has a one-page method with no local sleep, redirect/final-URL
    fence, typed valid-429 and 403 signals, and no follow-up event-detail request.
  - **Recovery and consistency:** a full page commits its normalized stage and releases to `paused`, then the
    workflow reclaims a fresh database lease for the next page. A short raw page becomes terminal and promotes
    canonical events, source observations, stage cleanup, and `succeeded` status in one transaction. A worker crash
    after terminal staging retains that stage; its later reclaim retries promotion without another source GET. A
    source revision change discards the old stage before a fresh admission, and duplicate remote IDs are retained for
    all source rows (including filtered records) so pagination drift fails closed. Legistar does not provide a remote
    snapshot token, so this prevents local mixed runs but does not claim a globally consistent provider snapshot
    across a long delay.
  - **Scope and evidence:** fixture-only unit tests cover one-page adapter behavior, exact per-page Pacer input,
    full-page reclaim, Pacer/provider waits, source-revision reset, no direct fallback, and post-stage crash/retry
    promotion. PostgreSQL integration proves app-role DML denial, pause/reclaim, generic-completion refusal, atomic
    terminal promotion, and an owner revision reset; Redis proves distinct page queue IDs share the API-origin
    bucket; Temporal time-skipping proves page advancement plus durable wait re-entry. Full verification is green:
    `make install && make up && make migrate && make test && make test-integration && make lint typecheck`
    (**459 unit tests; 150 ordinary integration tests; Ruff clean; strict mypy clean across 191 source files**).
  - **Explicit boundary:** no live Legistar request, source activation, provider credential/account, registration,
    payment, calendar write, or commit was made. Sunnyvale's later P15c and Alameda's later P15d extensions keep the
    same bounded posture; Oakland and all other multi-request catalog adapters remain on their prior bounded path
    until a source-specific P15b-style contract is reviewed.

- **2026-07-18 — P15c COMPLETE: Sunnyvale durable per-page Legistar refresh and P15b race hardening.** Migration
  `0089` expands only the existing cursor-creation capability's closed profile set to
  `sunnyvale-legistar-meetings` / `SUNNYVALE_LEGISTAR` alongside the already-reviewed San Jose pair, then records
  that execution-contract change in Sunnyvale's monotonic source revision. It adds no generic Legistar mode, new
  source seed, credential, or public-crawl activation. The shared adapter already pins Sunnyvale's exact
  `SunnyvaleCA/Events` endpoint, matching human handoff host, hidden-record exclusion, local 90-day OData window,
  five-page cap, and no-detail-fetch posture (FR-3.1/FR-3.8/FR-10.3/10.4, NFR-1/NFR-8, ADR-001/003/005).
  - **Stage/promotion race fence:** fixed-search-path `SECURITY DEFINER` trigger functions privately lock the
    source row and verify its current source revision, enabled/reviewed/handoff posture, exact key/mode pair, and
    page cap before a staged-page insert or a paged-run success transition. A source edit that commits after the GET
    but before staging is rejected before any stage write; an edit that commits before terminal promotion rolls back
    canonical merge, provenance, and success together. An owner edit that waits behind the promotion lock linearizes
    after that already-current transaction. The non-superuser app role has no direct access to the progress/stage
    tables or to these internal guards.
  - **Recovery semantics:** an explicit valid `Retry-After` still publishes shared Pacer backoff. A headerless or
    malformed 429 remains an ordinary untyped fetch failure—no unsupported provider delay is fabricated—but now
    pauses any earlier normalized pages and fails the workflow for a same-source/run retry rather than deleting the
    cursor. Both one-GET and paged catalog workflows likewise fail a `skipped` preflight rather than successfully
    consuming a still-due deterministic cadence slot; after an owner/policy correction, the existing
    `ALLOW_DUPLICATE_FAILED_ONLY` start policy restarts that exact identity safely.
  - **Evidence:** Sunnyvale fixtures cover one no-sleep page with exact offset, filtered-row remote-ID retention,
    typed valid 429/403, and generic headerless-429 behavior. Unit coverage proves direct-path refusal, page
    progress, retry without replaying page zero, and shared origin scope. PostgreSQL coverage proves only San Jose
    and Sunnyvale can prepare the paged capability; Sunnyvale terminal promotion is atomic; stage and promotion
    contract-race attempts are rejected/rolled back. Temporal coverage proves corrected skipped one-GET and
    Sunnyvale paged workflows reuse the same cadence identity; Redis proves the two cities contend on the exact
    origin bucket. Full verification is green: `make install && make up && make migrate && make test && make
    test-integration && make lint typecheck` (**463 unit tests; 156 ordinary integration tests; Ruff clean; strict
    mypy clean across 191 source files**), plus `make quality` and `make quality-load`.
  - **Explicit boundary:** no live Legistar request, source activation, provider credential/account, registration,
    payment, calendar write, or commit was made. At P15c close, Alameda, Oakland, and every other
    paginated/redirecting catalog adapter remained held; P15d later admits only Alameda's reviewed contract. Oakland
    and every other mode remain held; no scope was widened beyond the three exact anonymous civic profiles.

- **2026-07-18 — P15d COMPLETE: Alameda durable per-page Legistar refresh.** Migration `0090` expands the existing
  P15b/P15c cursor capability by exactly one profile: `alameda-legistar-meetings` / `ALAMEDA_LEGISTAR`. It records
  that execution-contract change in Alameda's monotonic source revision while retaining the closed San Jose and
  Sunnyvale pairs. At P15d close, Oakland and every other key/mode combination remained database-invalid; P15e later
  admits the reviewed Oakland pair. The existing shared adapter
  pins Alameda's `Alameda/Events` endpoint, matching `alameda.legistar.com` human handoff host, hidden-record
  exclusion, local 90-day OData window, five-page cap, and no-detail-fetch posture (FR-3.1/FR-3.8/FR-10.3/10.4,
  NFR-1/NFR-8, ADR-001/003/005).
  - **Current-contract fence:** `fn_prepare_paged_catalog_refresh` and the private stage/promotion guard now recognize
    only the three exact profiles. They retain the fixed search path, app-role execution boundary, source-revision,
    enabled/reviewed/handoff/page-cap checks, and row locks from P15c. An owner change before either write rejects the
    old contract; terminal promotion still rolls back canonical merge, provenance, and run success as one effect.
  - **Durable behavior / evidence:** the direct service and router refuse a legacy Alameda fetch; the paged Temporal
    workflow owns one Pacer admission per fixed page and a failed `skipped` preflight can restart the same deterministic
    cadence identity after correction. Offline fixtures prove `$skip=100`, no local sleep, filtered remote-ID
    retention, typed valid 429/403, and generic headerless-429 failure. PostgreSQL coverage proves all three exact
    profiles prepare, Alameda terminal stage/promotion is atomic, and Oakland remains invalid; Redis proves all three
    city queue identities share the `webapi.legistar.com` bucket. Full verification is green: `make install && make
    up && make migrate && make test && make test-integration && make lint typecheck` (**466 unit tests; 158 ordinary
    integration tests; Ruff clean; strict mypy clean across 191 source files**), plus `make quality` and `make
    quality-load`.
  - **Explicit boundary:** no live Legistar request, source activation, provider credential/account, registration,
    payment, calendar write, or commit was made. This is only the separately reviewed, credential-free Alameda
    factual-discovery path; it does not authorize broader redistribution. At P15d close, Oakland and every other
    paginated or redirecting catalog profile remained held; P15e later admits Oakland's reviewed contract.

- **2026-07-18 — P15e COMPLETE: Oakland durable per-page Legistar refresh and exact transport-profile hardening.**
  Migration `0091` admits only `oakland-legistar-meetings` / `OAKLAND_LEGISTAR` to the existing cursor and records
  the execution-contract change in Oakland's monotonic source revision. It also replaces the prepare and current-
  contract predicates for all four city profiles so each requires its exact source key, mode, `webapi.legistar.com`
  endpoint, and sole approved origin before a cursor, stage, or promotion can proceed. This adds no generic Legistar
  capability, source seed, credential, or public-crawl activation (FR-3.1/FR-3.8/FR-10.3/10.4, NFR-1/NFR-8,
  ADR-001/003/005).
  - **Closed Oakland contract:** the shared adapter fixes the `Oakland/Events` client path, matching
    `oakland.legistar.com` human handoff, local 90-day OData window, five-page cap, no-detail-fetch rule, and source-
    specific physical/hybrid filtering. Its P15e page seam performs exactly one shared-Pacer-admitted GET without a
    process-local sleep; raw source documents never reach the cursor ledger.
  - **Race and recovery evidence:** offline Oakland fixtures prove `$skip=100`, filtered remote-ID retention, typed
    valid 429/403, and generic headerless-429 failure. Unit coverage blocks direct/legacy routing. PostgreSQL proves
    all four exact profiles prepare, a drifted Oakland endpoint/origin is rejected before staging, Oakland terminal
    promotion is atomic, and owner revision races at stage and promotion reject and roll back effects for every city.
    Temporal proves a corrected `skipped` Oakland cadence identity restarts safely; Redis proves all four city queue
    identities contend in the same `webapi.legistar.com` bucket. Full verification is green: `make install && make
    up && make migrate && make test && make test-integration && make lint typecheck` (**469 unit tests; 167 ordinary
    integration tests; Ruff clean; strict mypy clean across 191 source files**), plus `make quality` and `make
    quality-load`.
  - **Explicit boundary:** no live Oakland request, source activation, provider credential/account, registration,
    payment, calendar write, or commit was made. The four city profiles are limited to separately reviewed,
    credential-free factual discovery with attributed handoffs; this does not authorize broader redistribution.
    Every other paginated or redirecting catalog profile remains held pending its own contract.

- **2026-07-18 — P16a COMPLETE: typed Google Calendar authorization versus transient quota classification.**
  Google Calendar now emits `GoogleCalendarReconsentRequiredError` only for an HTTP 401 or explicit credential and
  scope failures (`invalid_grant` and documented authorization reasons), preserving FR-9.7's one explicit
  re-consent action. A Google 403 with a documented rate/quota reason or `RESOURCE_EXHAUSTED`, and an HTTP 429,
  instead emits the distinct `GoogleCalendarRetryableError`; a non-specific 403 remains an ordinary adapter
  failure. The Events.list/watch sync adapter now calls the same shared classifier as CalendarPort mutations, so
  either adapter cannot silently drift from that safety boundary (FR-9.7, NFR-8).
  - **Evidence:** fixture-only `httpx.MockTransport` coverage proves a re-consent 403, every supported rate/quota
    reason, status-only `RESOURCE_EXHAUSTED`, a generic forbidden response, headerless 429, and sync-path rate
    limiting. Focused Calendar adapter/sync verification passes **28 tests**. `make install`, `make test`
    (**476 passed**), `make test-integration`, `make quality`, `make quality-load`, and `make lint typecheck` all
    pass against the local Docker services; the integration and quality targets each apply migrations as owner and
    exercise the application as the non-superuser `ec_app` role.
  - **Explicit boundary:** no Google OAuth flow, token refresh, production binding, Calendar request, account
    action, source egress, calendar write, or commit was made.

- **2026-07-18 — P16b COMPLETE: outbox contention accounting and current-lease notification authority.**
  The existing ADR-009 retry accounting is now explicitly retained and evidenced: claiming, `BUSY`, and
  post-send lost-ledger deferrals never consume `attempt_count`; only a known `NotificationPort.send()` exception
  spends the bounded delivery-failure budget. The relay additionally refuses a new
  `NotificationClaim.LEASE_LOST` outcome without sending, acknowledging, or rescheduling. Before it can acquire a
  durable notification slot, the PostgreSQL repository locks the exact outbox row and verifies its matching,
  unexpired token and non-delivered/non-terminal state. It uses wall-clock checks and atomically reserves a fresh
  bounded send window both before and after ledger acquisition; the ledger lease is no later than that owned outbox
  lease. Immediately before provider I/O, the relay checks both current leases again, so a pause after admission
  cannot emit a call once the lease has expired or been reclaimed. The unavoidable check-to-effect crash/pause
  window remains covered by the stable notification dedup key and the `NotificationPort` at-most-once-visible
  contract (FR-6.6, FR-8.9, NFR-8, ADR-009).
  - **Evidence:** unit coverage proves the durable-`DELIVERED` acknowledgement and `LEASE_LOST` no-send branches,
    in addition to the existing repeated-BUSY and lost-ledger budget tests. PostgreSQL coverage delivers a real
    notification, reopens only its outbox acknowledgement, and proves a fresh non-deduplicating port receives no
    second call because the durable ledger re-acknowledges it. It also proves expired, reclaimed, and terminal
    records cannot create a ledger slot while the current owner can, with the ledger expiry no later than the
    outbox lease and a nearly expired current lease renewed to a full 60-second send window; the pre-send fence
    rejects an expired lease. Focused outbox verification passes **11 unit and 4 integration tests**. `make
    install`, `make test` (**479 passed**), `make test-integration`, `make quality`, `make quality-load`, and
    `make lint typecheck` all pass against the local Docker services; integration and quality targets apply
    migrations as owner and run the application through the non-superuser `ec_app` role.
  - **Explicit boundary:** no real notifier, email/domain, provider credential, Calendar action, source egress, or
    commit was made.

- **2026-07-18 — P17 COMPLETE: tenant-isolated implicit feed feedback and next-feed re-score.** The authenticated
  `POST /v1/feed-feedback` contract accepts only a caller-minted `signal_id`, a canonical event id, and one closed
  `scroll` / `dwell` / `click` / `like` / `dismiss` kind; it rejects caller tenants, raw event text, durations,
  and weights. Migration `0113` adds the explicit positive `like` receipt kind without changing the bounded
  server-owned +1.0 influence ceiling shared with `click`.
  `RankingFeedbackService` resolves the canonical event before deriving a bounded lexical feature snapshot, and the
  ranker reads a feedback-aware profile overlay on the next feed. This closes the FR-2.1 / FR-4.3 implicit-signal
  path without read-modify-writing the existing revisioned declared-profile row (ADR-001, NFR-7/8).
  - **Durable receipt boundary:** migration `0092` adds append-only `tenant_ranking_feedback_receipts`, keyed by
    `(tenant_id, signal_id)`, with tenant and canonical-event foreign keys, FORCE RLS using the exact fail-closed
    `NULLIF(current_setting('app.tenant_id', true), '')::uuid` predicate, and only `SELECT, INSERT` for the
    non-superuser `ec_app` role. The first server-derived delta map is immutable. An exact logical retry (same
    canonical event and kind) replays without a second effect even if later catalog enrichment changes the event
    description or venue; a reused key with a different event or kind returns a conflict. Aggregates are recomputed
    from committed receipts and clamped per feature, so concurrent distinct signals cannot lose increments and one
    tenant cannot grow a single learned feature's numeric influence without bound. Receipt volume and feature-key
    cardinality are intentionally not capped by P17; an owner-approved retention, rate-limit, or rollup policy is
    required before making a scale/availability claim for this synchronous aggregate read.
  - **Bounded baseline / intentionally narrow semantics:** fixed server-owned total deltas are scroll `+0.25`, dwell
    `+0.75`, click `+1.0`, and dismiss `-1.0`, divided over at most 24 first-seen lowercase ASCII public-event
    terms (each at most 64 characters); no client-selected score crosses the boundary. These are a provisional P17
    implementation baseline, not an approved learned taxonomy, dwell threshold, decay, model-training, retention,
    or exposure-verification policy. Until a signed served-item capability exists, the API treats them as bounded
    self-reported preferences rather than proof that a feed item was actually viewed. The future FR-10.5/NFR-11
    erasure workflow must explicitly enumerate this behavioral-data table; P17 does not claim that broader erasure
    work is complete.
  - **Evidence:** pure and in-memory tests prove normalized/bounded derivation, positive and negative signal polarity,
    exact replay/no double aggregate, conflicting signal reuse, catalog-enrichment retry convergence, tenant-local
    aggregate bounds, and a Jazz dwell that changes a later equal-base-score feed from Python-first to Jazz-first.
    PostgreSQL tests prove owner/other/unset/empty RLS contexts, cross-tenant `WITH CHECK` rejection, malformed-JSON
    rejection, immutable app-role grants, durable replay/conflict, first-snapshot retention, and a catalog-seeded
    persisted click that materially reorders a later real `PersonalizedRanker` result. API coverage proves
    authentication, replay, conflict, canonical-target validation, and body tenant/weight rejection. Final full
    verification passes: `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` (**486 passed, 175 deselected**; Ruff clean; strict mypy clean across **196 source files**), plus
    `make quality` and `make quality-load`; `alembic heads` reports `0092 (head)`.
  - **Explicit boundary:** no browser/UI scroll capture, external source call, registration, Calendar action, live
    model, model training, credential/account action, exposure token, retention scheduler, erasure workflow, or
    commit was made.

- **2026-07-18 — P18 COMPLETE: explicit Cohere and Google Calendar adapter selection.** The existing concrete
  adapters are now reachable from the composition root without changing the offline default graph (FR-4.2,
  FR-9.1/9.6). `EC_COHERE_API_KEY` is a `SecretStr`; when nonempty it selects
  `CohereRerankCrossEncoder` with configurable model/timeout, while no key keeps the deterministic cross-encoder.
  `EC_GOOGLE_CALENDAR_ENABLED=false` keeps `MockCalendar`; true selects `GoogleCalendarAdapter` only after a
  tenant-scoped `GoogleCalendarAccessPort` is supplied.
  - **Safe Google bootstrap:** a direct composition override may supply the access/binding/client ports, or the
    normal API and Temporal worker entrypoints can resolve an owner-controlled trusted local
    `EC_GOOGLE_CALENDAR_ACCESS_FACTORY=module:callable`. That callable returns the existing typed per-tenant access
    port; it is deliberately not a global bearer-token setting and does not implement OAuth, refresh-token storage,
    consent, or calendar creation. RLS-backed `PostgresGoogleCalendarBindings` remains the default binding source.
    An enabled flag with neither access injection nor factory fails closed at startup rather than silently using a
    mock or an unsafe credential path.
  - **Composition precedence / offline posture:** explicit `ranker` and `calendar` root overrides remain higher
    authority than process settings, as for the existing test/provisioning seams. Provider selection is independent
    of `EC_MOCK_CLOUD`, so fixture transports can exercise real adapter request shaping while all other cloud ports
    stay mocked; adapter construction itself makes no network call and caller-injected `httpx.AsyncClient` ownership
    remains with the caller.
  - **Evidence:** offline composition tests prove default deterministic/Mock selection; explicit settings construct
    Cohere and Google adapters and issue only `httpx.MockTransport` Cohere/Google fixture requests; the trusted
    factory path reaches Google through settings; missing Google access fails closed; direct override precedence,
    `EC_` environment parsing, and blank Cohere-key rejection are locked. `.env.example` and README now document the
    opt-in settings and no-global-token boundary. Final full verification passes: `make install && make up && make
    migrate && make test && make test-integration && make lint typecheck` (**492 passed, 175 deselected**; **174
    integration tests passed**; Ruff clean; strict mypy clean across **196 source files**), plus `make quality` and
    `make quality-load`; `alembic heads` reports `0092 (head)`.
  - **Explicit boundary:** no Cohere key, Google OAuth client, access/refresh token, calendar binding, external
    request, account action, Calendar mutation, source egress, or commit was made. A real deployment still needs an
    owner-provisioned tenant-scoped access-port factory before enabling Google.

- **2026-07-18 — P19 COMPLETE: Handoff-D safety-regression closure (Meetup intentionally held).** The existing
  durable outbox coverage was audited to prove the `DELIVERED` ledger short-circuit acknowledges without notifying,
  `BUSY` reschedules without consuming delivery budget or sending, and a manually reopened delivered row is stopped
  by the durable PostgreSQL ledger rather than mock-notifier memory (FR-8.9, ADR-009). The existing real Temporal
  midflight kill-switch regression also proves the second data-plane policy check prevents a source mutation and
  routes to handoff (AC-40/50, ADR-003/004).
  - **Fail-closed registration:** new unit regressions show both `UNKNOWN` membership and an ordinary membership
    read outage remove `AUTONOMOUS_SLA`, route to handoff, and make zero `register()` attempts/effects (FR-5.2,
    ADR-003/005). A real bug was fixed in confirmation verification: an opaque wake-up followed by a fresh
    eventually-consistent `NOT_PRESENT` source read now remains `PENDING`/`AWAITING_CONFIRMATION`, just like a
    still-pending read, until a later independent `CONFIRMED` read. Unit and Temporal/PostgreSQL regressions prove
    no Calendar entry or `lifecycle.registered` outbox row is created before that confirmation (FR-8.4, FR-16.1,
    ADR-003/011).
  - **Provider defensive contracts:** the successful Google insert path remains explicitly no-PATCH, and a new
    no-I/O bare-offset-zone regression rejects anything but an IANA time zone (FR-9.5). Cohere fixture coverage now
    exercises non-object/missing-or-wrong `results`, malformed result/index/score, duplicate index, out-of-range,
    non-finite, and incomplete response paths, all fail closed (FR-4.2). Meetup-specific D tests remain deliberately
    unrun and unchanged per the owner instruction to exclude Meetup.
  - **Verification:** `make install && make up && make migrate && make test && make test-integration && make lint
    typecheck` passes (**501 passed, 176 deselected**; **175 integration tests passed**; Ruff clean; strict mypy
    clean across **196 source files**), plus `make quality` and `make quality-load`; `alembic heads` reports
    `0092 (head)`.
  - **Explicit boundary:** no external source request, Meetup action, credential/account action, real Cohere/Google
    request, RSVP, payment, Calendar mutation, migration, or commit was made.

- **2026-07-18 — P20 HELD: calendar upsert/transition recovery semantics need an owner decision.** An optional
  Handoff-E split was designed and adversarially reviewed but deliberately not landed. If its scheduling-only retry
  budget exhausts after a successful Calendar upsert, a simple failed-child outcome would violate ADR-007's
  one-nonterminal-lifecycle ↔ one-open-workflow invariant and can leave the parent waiting for a child outcome.
  The read-only invariant scanner and closed-workflow organizer-change repair path do not own this missing schedule
  transition. The code remains on the prior combined `write_to_calendar` behavior rather than introducing that unsafe
  state.
  - **Owner choice required before implementation:** ratify either (a) a durable open-child schedule-repair/retry
    protocol and its parent-visible result, (b) a distinct authorized repair/terminal state and operator task, or
    (c) an explicit orphan-repair policy. Each changes settled ADR-007 recovery semantics; none is inferred here.
  - **Explicit boundary:** no provider request, credential/account action, Calendar mutation, migration, or commit
    was made for this held item.

- **2026-07-18 — P21 COMPLETE: durable lifecycle-watch projection ACK-loss recovery and live-lease authority.**
  The ADR-008 projection relay now has both offline protocol coverage and a real PostgreSQL recovery regression for
  its effect-first / acknowledge-second boundary. A guarded lifecycle transition creates the opaque `register`
  projection; the test records a post-activation public change, lets the real tenant-scoped registry commit the
  subscription, global watch, and one catch-up delivery, then injects a crash before the projection acknowledgement.
  The durable record remains undelivered and is reclaimed under a new token; replay preserves the persisted
  `active_since` cutoff, receives the registry's idempotent stale result, and only the fresh lease acknowledges it.
  The result stays exactly one subscription, one global watch, and one catch-up delivery (FR-8.7a/8.9, NFR-8,
  ADR-007/008).
  - **Current-lease fence:** migration `0093` replaces only
    `fn_mark_watch_projection_delivered` and `fn_watch_projection_reschedule`, preserving their fixed
    `SECURITY DEFINER`/search-path/grant boundary while requiring
    `lease_expires_at > clock_timestamp()`. An expired original token can neither acknowledge nor reschedule before
    a new claimant rotates it; both operations are proven true no-ops, including retry timing and error state.
    Downgrade restores the prior `0092` function bodies, and the local `0093 → 0092 → head` round trip passes.
  - **Evidence:** focused unit projection recovery tests and the real PostgreSQL/RLS integration regression pass;
    `make install`, `make up`, `make migrate`, `make test` (**503 passed, 177 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **196 source files**),
    `make quality`, and `make quality-load` all pass. `alembic heads` reports `0093 (head)`.
  - **Explicit boundary:** no source egress, provider request, credential/account action, Calendar mutation,
    external notification, or commit was made. P20 remains held pending the ADR-007 owner choice above.

- **2026-07-18 — P22 COMPLETE: ADR-008 fanout and closed-workflow repair live-lease authority.** The adjacent
  event-change delivery and delayed Calendar-repair control planes now use the same expiry boundary as P21: a
  matching opaque token loses terminal/retry authority at its database lease expiry, even before another worker
  reclaims the row (NFR-8, ADR-008). Migration `0094` replaces only the five existing guarded capabilities—fanout
  acknowledge/release, atomic closed-workflow delivery-to-repair enqueue, and repair acknowledge/reschedule—with
  the unchanged signatures, validation, `SECURITY DEFINER` search path, grant surface, and stored backoff; each
  terminal `UPDATE` now additionally requires `lease_expires_at > clock_timestamp()`. Downgrade restores the
  `0093`/`0063` exact-token-only bodies.
  - **Recovery evidence:** a real PostgreSQL integration regression makes four independent event deliveries and two
    independent repair rows ready under owner-only fixture setup, expires each exact lease before any reclaim, then
    calls the actual `ec_app` capabilities. Delivery ACK, delivery release, delivery-to-repair enqueue, repair ACK,
    and repair release each return false without changing any durable queue field; the expired enqueue creates no
    repair. Fresh claims rotate every token and the ordinary acknowledge/retry/enqueue operations then succeed.
    The read-only `fn_calendar_repair_change_applied` intentionally remains unchanged because its `false` result
    means “not applied,” not “lost lease”; changing that would require a separate tri-state contract decision.
  - **Verification:** the focused regression and the full change-detection module pass; `make install`, `make up`,
    `make migrate`, `make test` (**503 passed, 178 deselected**), `make test-integration`, `make lint typecheck`
    (Ruff clean; strict mypy clean across **196 source files**), `make quality`, and `make quality-load` all pass.
    `alembic heads` reports `0094 (head)`.
  - **Explicit boundary:** no source egress, provider request, credential/account action, Calendar mutation,
    external notification, worker-policy change, or commit was made. The broader request-start, notifier, polling,
    and catalog-queue lease audit remains separate follow-on work; P20 remains held pending its ADR-007 owner
    choice.

- **2026-07-18 — P23 COMPLETE: request-start relay live-lease authority.** The direct PostgreSQL request-start
  adapter now fences both terminal mutations with `lease_expires_at > clock_timestamp()`: a relay may acknowledge
  a Temporal start and advance the RLS-protected request to `started`, or release a failed start attempt with new
  retry state, only while it still holds its exact live lease (FR-6.8, NFR-8, ADR-003). This is an adapter-only
  correction; request-start intentionally remains an opaque direct-DML global queue, so no migration, trigger,
  workflow behavior, or grant surface changed.
  - **Recovery evidence:** a real PostgreSQL regression creates two isolated start instructions, claims each
    directly, and owner-expires each lease before reclaim. A stale acknowledgement and stale retry both return false
    with complete durable queue state unchanged; the stale acknowledgement also leaves the request in `received`.
    Fresh claims rotate both tokens and ordinary acknowledgement/retry then succeed. The existing deterministic
    Temporal parent-id/reject-duplicate recovery remains unchanged.
  - **Verification:** focused and full request-start integration coverage pass; `make install`, `make up`,
    `make migrate`, `make test` (**503 passed, 179 deselected**), `make test-integration`, `make lint typecheck`
    (Ruff clean; strict mypy clean across **196 source files**), `make quality`, and `make quality-load` all pass.
    `alembic heads` remains `0094 (head)`.
  - **Explicit boundary:** no provider request, Temporal production action, source egress, credential/account
    action, worker-policy change, migration, or commit was made. The notifier's post-send success-ACK posture is a
    separate ADR-009 decision; only its unambiguous expired failure/release paths may be addressed independently.

- **2026-07-18 — P24a COMPLETE: notifier failure/release live-lease authority.** The ADR-009 relay's two
  unambiguous failed-send cleanup paths now lose authority exactly at their database lease expiry, before any new
  relay has to reclaim the row (FR-8.9, NFR-8). `PostgresOutboxRepository.reschedule` fences both bounded retry and
  terminal-failure updates by the exact outbox id/token, nonterminal state, and
  `lease_expires_at > pg_catalog.clock_timestamp()`. `release_notification` likewise requires the exact live
  `sending` ledger binding (dedup key, outbox id, tenant, token) and a matching live, nonterminal outbox lease.
  This is an adapter-only correction; no schema, migration, worker policy, or external notifier behavior changed.
  - **Recovery evidence:** real PostgreSQL regressions owner-seed isolated opaque rows, then exercise the actual
    application-role repository. With both leases expired before reclaim, stale provider-failure cleanup cannot
    release the ledger, consume an attempt, move retry time, or terminalize either independent retry or
    terminal-failure row; complete durable outbox and ledger snapshots remain unchanged. Fresh claims rotate the
    tokens and the ordinary release/retry and release/terminal paths succeed. A separate cross-owner case proves a
    still-live old ledger cannot be released after its outbox lease has been reclaimed under a new token but before
    the new owner has claimed the ledger.
  - **Deliberate boundary:** post-send success acknowledgements (`mark_notification_delivered` and
    `mark_delivered`) are intentionally unchanged. ADR-009 needs an owner decision before altering them: either
    apply the same strict live-lease fence and rely on stable notification deduplication during recovery, or ratify
    and document a narrowly bounded late-success-ACK exception. P24a changes only the failure paths for which stale
    authority was unambiguously unsafe.
  - **Verification:** focused stale-owner regression (**2 passed**) and the complete outbox integration module
    (**6 passed**) pass; `make install`, `make up`, `make migrate`, `make test` (**503 passed, 181 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **196 source files**),
    `make quality`, and `make quality-load` all pass. `alembic heads` remains `0094 (head)`.
  - **Explicit boundary:** no provider request, external notification, credential/account action, source egress,
    Calendar mutation, Temporal production action, migration, or commit was made.

- **2026-07-18 — P25 COMPLETE: catalog-refresh terminal live-lease authority.** The catalog control plane now
  treats database lease expiry as an immediate loss of authority for its three direct terminal capabilities
  (FR-10.3/10.4, NFR-1/NFR-8, ADR-001/003/005). Migration `0095` replaces only
  `fn_complete_catalog_refresh`, `fn_fail_catalog_refresh`, and `fn_abort_paged_catalog_refresh`, requiring the
  exact running token and `lease_expires_at > pg_catalog.clock_timestamp()` before a generic run can advance its
  cadence, record a failure, or a P15 worker can discard a normalized cursor/stage. The existing generic
  `NOT EXISTS catalog_refresh_progress` completion guard, function signatures, validation, fixed search paths,
  `SECURITY DEFINER` boundary, and app-role grants remain intact.
  - **Recovery evidence:** real PostgreSQL regressions use the migration owner only to expire isolated leases and
    exercise the actual `ec_app` capabilities. Before any reclaim, stale generic complete and fail calls return
    false with full run snapshots unchanged; fresh claims rotate their tokens and then complete/fail normally. A
    terminal P15 stage retains both its progress cursor and staged page when a stale abort returns false; a fresh
    claimant reads that same terminal cursor and can then deliberately abort/clean it up. The `0095 → 0094 → head`
    function-only migration round trip and focused regressions pass.
  - **Deliberate boundary:** P15's stage and promotion functions already verify live ownership on entry, but their
    final in-transaction writes need a separate TOCTOU/rollback analysis; P25 changes only the deterministic
    expired-before-reclaim terminal calls. No source-read policy, Pacer behavior, paging contract, or provider
    adapter changed.
  - **Verification:** focused stale-owner regression (**2 passed**); `make install`, `make up`, `make migrate`,
    `make test` (**503 passed, 183 deselected**), `make test-integration`, `make lint typecheck` (Ruff clean;
    strict mypy clean across **196 source files**), `make quality`, and `make quality-load` all pass. `alembic
    heads` reports `0095 (head)`.
  - **Explicit boundary:** no external source request, provider mutation, source activation, credential/account
    action, registration, payment, Calendar mutation, Temporal production action, or commit was made.

- **2026-07-18 — P26 COMPLETE: public watch-poll terminal live-lease authority.** The fixture-only ADR-008 public
  cursor now treats database expiry as loss of authority for both its success acknowledgement and its failed-poll
  release (FR-8.7a, NFR-8/NFR-17, ADR-008). Migration `0096` replaces only
  `fn_mark_watch_poll_succeeded` and `fn_release_watch_poll`, retaining their source/token/time/error validation,
  fixed `SECURITY DEFINER` search path, and capability grants while requiring
  `lease_expires_at > pg_catalog.clock_timestamp()` on the exact cursor row. The claim/list capabilities and
  redacted state projection are unchanged.
  - **Recovery evidence:** a real PostgreSQL regression creates separate active public success and failure cursors,
    claims them through the `ec_app` repository, then owner-expires each exact lease before any reclaim. A stale
    success cannot advance next due/reset failure evidence, and a stale release cannot record a failure or schedule
    the cursor; full owner-only mutable snapshots remain unchanged. Fresh claims rotate both opaque tokens and their
    ordinary success/failure transitions then complete. The existing scheduler ACK-loss path remains the semantic
    recovery proof: normalized observations are persisted before cursor acknowledgement, so a lost result lease
    re-polls and converges through the durable change fingerprint/delivery keys rather than duplicating a change.
  - **Operational note:** the database, not the caller-supplied completion timestamp, decides live authority. The
    production scheduler uses a wall clock; a materially stale injected caller clock therefore fails closed at its
    result write rather than modifying an expired cursor.
  - **Verification:** the full watch-poll PostgreSQL module passes (**4 passed**), its `0096 → 0095 → head` function
    migration round trip passes, and `make install`, `make up`, `make migrate`, `make test` (**503 passed, 184
    deselected**), `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **196 source
    files**), `make quality`, and `make quality-load` all pass. `alembic heads` reports `0096 (head)`.
  - **Explicit boundary:** no source poll, provider request, cadence activation, credential/account action,
    registration, payment, Calendar mutation, external notification, or commit was made.

- **2026-07-18 — P27a COMPLETE: handoff-expiry queue terminal live-lease authority.** The ADR-007 orphan-repair
  queue now treats database lease expiry as an immediate loss of authority for its two direct queue mutations.
  `PostgresHandoffExpiryRepository.acknowledge` and `.reschedule` both require the exact task/token, an unresolved
  row, and `lease_expires_at > pg_catalog.clock_timestamp()`; a delayed repair worker can neither resolve a queue
  instruction nor alter its retry/error state before a fresh worker reclaims it. This is an adapter-only correction
  to the opaque global repair queue: its schema, grants, queue grace, normal Temporal TTL path, lifecycle graph,
  and worker policy are unchanged (FR-6.6, NFR-8, ADR-007).
  - **Recovery evidence:** a real PostgreSQL regression creates a post-grace due task, claims it through the actual
    `ec_app` adapter, and expires that exact token before any reclaim. Both stale acknowledgement and stale retry
    return false with a complete queue snapshot unchanged. A fresh claim rotates the token, then the ordinary
    retry, reclaim, and acknowledgement paths succeed. The normal guarded lifecycle expiry is also completed for
    the fixture, preserving the task/lifecycle coupling.
  - **Deliberate boundary:** this closes only direct queue acknowledgement/retry writes. The separate stale-worker
    lifecycle effect remains a queue-plus-`fn_transition` atomicity problem: a worker that crosses expiry after its
    liveness check must not be fenced with a racy preflight. The next slice will introduce a narrow queue-aware
    capability for the orphan-repair route while leaving the normal Temporal TTL activity unmodified; it needs no
    provider or owner-policy change, but requires its own migration and cross-owner race proof. Notifier post-send
    success acknowledgements remain the separate ADR-009 owner decision recorded in P24a.
  - **Verification:** focused handoff-expiry integration coverage (**7 passed**); `make install`, `make up`, `make
    migrate`, `make test` (**503 passed, 185 deselected**), `make test-integration`, and `make lint typecheck`
    pass (Ruff clean; strict mypy clean across **196 source files**), plus `make quality` and `make quality-load`.
    `alembic heads` remains `0096 (head)`.
  - **Explicit boundary:** no source egress, provider request, credential/account action, registration, payment,
    Calendar mutation, external notification, Temporal production action, migration, or commit was made.

- **2026-07-18 — P27b COMPLETE: atomic orphan handoff-expiry effect fencing.** The ADR-007 orphan-repair route
  now has a narrow, queue-lease-fenced capability instead of a racy liveness preflight (FR-6.6, FR-8.9, NFR-8,
  ADR-007). Migration `0097` adds `fn_expire_handoff_from_queue(task_id, lease_token)`, available only to the
  application role under the system control-plane context. It derives the lifecycle and handoff-task identity from
  the exact leased queue row, runs the existing guarded expiry transition in a nested transaction, then locks and
  rechecks that same queue row before resolving it. If the lease was reclaimed or expired while the guarded work
  waited, the private lease-loss path rolls back its provisional lifecycle, task, ledger, and outbox writes as one
  unit. The regular Temporal TTL expiry capability remains unchanged and independent of the orphan queue lease.
  - **Recovery evidence:** real `ec_app` PostgreSQL regressions cover stale authority before reclaim, expiry while
    the guarded transaction is blocked, and cross-owner reclaim after worker liveness has already been observed.
    Each proves the stale worker leaves lifecycle, task, queue, ledger, and outbox state unchanged; a fresh lease
    then produces exactly one terminal lifecycle effect and notification. The suite also proves ordinary Temporal
    expiry still resolves a handoff whose orphan queue row happens to be claimed, and that no queue lease token is
    emitted in the outbox payload.
  - **Implementation boundary:** the migration changes the generic transition guard only to defer the one exact
    queue row while this private orphan-repair capability is active; invalid or non-expiry use is rejected, and all
    normal transition callers retain the prior cleanup behavior. P27a's direct stale acknowledgement/retry fencing
    remains in force. This does not add any external source, provider, notification delivery, credential, Calendar,
    or Temporal production integration.
  - **Verification:** focused unit handoff-expiry coverage (**7 passed**) and focused PostgreSQL coverage
    (**11 passed**); `make install`, `make up`, `make migrate`, `make test` (**505 passed, 189 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **196 source files**),
    `make quality`, and `make quality-load` all pass. A manual `0097 → 0096 → 0097` migration round trip and
    `alembic heads` both confirm `0097 (head)`.
  - **Explicit boundary:** only the local test database migration was applied; no source egress, provider request,
    credential/account action, registration, payment, Calendar mutation, external notification delivery, Temporal
    production action, or commit was made.

- **2026-07-18 — P28 COMPLETE: paged catalog-refresh final lease fencing.** P15's durable Legistar spine now
  treats expiry during its final database waits as loss of authority rather than as permission to advance a stale
  cursor or mark a stale catalog run successful (FR-10.3/10.4, NFR-8, ADR-001/003/005). Migration `0098` replaces
  only `fn_stage_paged_catalog_refresh_page` and `fn_promote_paged_catalog_refresh`, preserving their fixed
  signatures, `SECURITY DEFINER` boundary, current reviewed-profile contract guards, and `ec_app` capability grants.
  - **Full-page rollback:** after taking the run row lock, the stage capability reads the live clock and encloses its
    page facts, normalized candidates/event ids, cursor advance, and `running → paused` write in one nested SQL
    subtransaction. A final exact-token live-lease failure raises/catches only private `EC002`, returns
    `lease_lost`, and rolls those provisional page/cursor writes back. The application already maps that result to
    a recoverable busy outcome.
  - **Promotion fence:** after count/provenance validation, promotion reacquires and rechecks the exact run row.
    Its success update retains a live-clock predicate, and the new
    `trg_catalog_refresh_promotion_lease` runs alphabetically after the existing source-contract trigger. That
    final trigger observes time only after a possible source-row contract wait and suppresses a late
    `running → succeeded` update, so the function returns false and the real promoter's caller-owned transaction
    rolls its catalog/provenance work back. No source/profile policy or Pacer behavior changed.
  - **Recovery evidence:** real `ec_app` PostgreSQL races hold the reviewed source row across a full-page contract
    wait and prove the expired worker leaves zero new stage facts and an unadvanced cursor; a fresh lease stages
    page zero once. The terminal promotion capability is separately blocked at both its final run-row lock and its
    post-contract source-row lock, then returns false after expiry with the complete terminal stage intact. A fresh
    claim and the real promoter complete that same stage once. The full catalog-source PostgreSQL module remains
    green, including its existing non-empty promoter rollback coverage.
  - **Deliberate boundary:** a short terminal page has no final run-state mutation. Its normalized stage remains
    the prior recoverable crash-retry behavior; this slice does not silently broaden into a policy that rejects all
    late terminal-stage writes. P28 fences the nonterminal cursor release and terminal publication paths only.
  - **Verification:** focused P28 PostgreSQL races (**3 passed**) and the full catalog-source module (**26 passed**);
    `make install`, `make up`, `make migrate`, `make test` (**505 passed, 192 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **196 source files**),
    `make quality`, and `make quality-load` all pass. A local `0098 → 0097 → 0098` round trip and `alembic heads`
    confirm `0098 (head)`.
  - **Explicit boundary:** only the local test database migration was applied; no source egress, provider request,
    source activation, credential/account action, registration, payment, Calendar mutation, external notification
    delivery, Temporal production action, or commit was made.

- **2026-07-18 — P29 COMPLETE: late terminal-stage recovery regression.** P28 deliberately retains a short
  terminal page that reaches its normalized stage after the worker's lease expires: it is a recoverable input, not
  a publication effect. A real PostgreSQL regression now holds the reviewed source row through the stage-contract
  trigger, lets the application-role lease expire on the database clock, then releases the blocked terminal stage.
  The durable cursor/page/candidate/remote-id facts remain intact while the stale run remains unpublished. A fresh
  lease reopens that exact terminal input and the real promoter creates its canonical event and source observation
  once; a succeeding replay reports the already-succeeded run and leaves one observation (FR-3.8, FR-10.3/10.4,
  NFR-8, ADR-001/003/005).
  - **Deliberate boundary:** this is a fixture-only recovery proof for P28's stated terminal-stage behavior, not a
    new late-write policy. The full-page cursor release and terminal publication remain fenced by P28; rejecting all
    late terminal staging would be a separate behavioral decision and was not inferred. No source egress, provider
    request, credential/account action, registration, payment, Calendar mutation, notification delivery, or commit
    was made.
  - **Verification:** focused PostgreSQL regression (**1 passed**) and the full catalog-source module (**27 passed**)
    pass; `make install`, `make up`, `make migrate`, `make test` (**505 passed, 193 deselected**),
    `make test-integration` (**192 passed**), `make lint typecheck` (Ruff clean; strict mypy clean across
    **196 source files**), `make quality`, and `make quality-load` all pass. `alembic heads` remains
    `0098 (head)`.

- **2026-07-18 — P30 COMPLETE: paged catalog-prepare pre-egress lease fencing.** Migration `0099` replaces only
  `fn_prepare_paged_catalog_refresh`, retaining its four exact reviewed Legistar profiles, signature,
  `SECURITY DEFINER`/fixed search path, and app-role grant. The capability refreshes the database clock after its
  refresh-run `FOR UPDATE` wait, then wraps cursor lookup/reset/creation in a nested SQL subtransaction. It checks
  the still-locked exact lease both after the cursor wait and after any cursor write; private `EC003` rolls a late
  mutation back and returns `lease_lost`. A stale activity therefore cannot create/reset a cursor or reach Pacer
  and its source GET after a database wait (FR-3.8, FR-10.3/10.4, NFR-8, ADR-001/003/005).
  - **Recovery evidence:** real `ec_app` races hold first the refresh run and then an existing cursor row through
    expiry. The stale prepare returns `lease_lost`; the first case leaves no cursor and the second retains its prior
    cursor unchanged. Fresh claims prepare normally. A deterministic application regression proves that a
    `lease_lost` preparation returns `BUSY` before any Pacer admission, fetch, or promotion call. The full
    catalog-source PostgreSQL module passes (**29 passed**), and `0099 → 0098 → 0099` plus focused lock races pass.
  - **Deliberate boundary:** P29's late short-terminal-stage recovery behavior is unchanged: it preserves facts
    from an already-authorized GET for later exactly-once promotion. P30 runs strictly before Pacer/source I/O, where
    no such fact exists to salvage. No source egress, provider request, credential/account action, registration,
    payment, Calendar mutation, notification delivery, or commit was made.
  - **Verification:** focused unit (**1 passed**) and PostgreSQL (**2 passed**) regressions, the full
    catalog-source module (**29 passed**), and the `0099 → 0098 → 0099` round trip pass. `make install`, `make up`,
    `make migrate`, `make test` (**506 passed, 195 deselected**), `make test-integration` (**194 passed**),
    `make lint typecheck` (Ruff clean; strict mypy clean across **196 source files**), `make quality`, and
    `make quality-load` all pass. `alembic heads` reports `0099 (head)`.

- **2026-07-18 — P31 COMPLETE: atomic generic catalog-refresh publication.** The legacy one-shot source path no
  longer commits canonical events, provenance, and refresh success in separate transactions. A new inward
  `CatalogRefreshCommitter` port and `PostgresCatalogRefreshCommitter` adapter compute embeddings before opening one
  tenant-neutral transaction, then merge canonical rows/source links, write normalized observations, and invoke the
  existing database-clock `fn_complete_catalog_refresh` capability as its final action (FR-3.8, FR-10.3, NFR-8,
  ADR-001/003). A rejected final guard raises only a private adapter sentinel; the transaction rolls every
  provisional publication write back and the port returns no commit result, which the service projects as recoverable
  `BUSY` rather than falling back to the old split writes.
  - **Recovery evidence:** a real `ec_app` PostgreSQL race locks an existing duplicate canonical row while claimant
    A blocks in its merge. After the database clock expires, claimant B rotates the same run lease; releasing A makes
    its final guard reject and rolls back A's enrichment, source link, and observation. B then publishes exactly one
    fresh event observation; a replay under the completed token produces no additional catalog/provenance effect.
    The offline service regression also proves a rejected committer result is `BUSY` with no non-atomic fallback.
  - **Implementation boundary:** embeddings remain outside the database transaction so a provisioned model never
    holds catalog locks. No migration, source policy/profile, Pacer behavior, external request, provider mutation,
    credential/account action, registration, payment, Calendar mutation, notification delivery, Temporal production
    action, or commit was made.
  - **Verification:** focused catalog unit coverage (**38 passed**) and the fixture-only PostgreSQL reclaim race
    (**1 passed**) pass. `make install`, `make up`, `make migrate`, `make test` (**509 passed, 196 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **197 source files**),
    `make quality` (**1 passed**), and `make quality-load` (**1 passed, 1 deselected**) all pass. `alembic heads`
    remains `0099 (head)`.

- **2026-07-18 — P32 COMPLETE: post-Pacer paged pre-GET lease revalidation.** P30 fenced initial paged preparation
  before Pacer admission; this slice closes the remaining interval between a granted shared Pacer lease and the one
  reviewed Legistar GET. After owner/source/policy rechecks, `PagedCatalogRefreshService` reruns the existing
  database-clock `prepare_paged_refresh` capability immediately before `fetch_page` (FR-10.3/10.4, NFR-8,
  ADR-003/005). A now-stale lease returns `BUSY` with no GET, stage, or promotion. If an at-least-once peer completed
  a recoverable terminal stage while this activity waited for Pacer, the current activity promotes that durable input
  instead of requesting the same page again.
  - **Recovery evidence:** deterministic offline regressions revoke the fixture lease during Pacer admission and
    prove zero source/catalog effects, then separately make the cursor terminal during that admission and prove one
    promotion with zero source GETs. This reuses P30's idempotent, DB-clock-fenced capability; no migration or new
    provider/source profile is required.
  - **Implementation boundary:** the four already-reviewed P15b/P15c/P15d/P15e Legistar profiles, their staging
    policy, P29's late-terminal recovery behavior, source policy, and Pacer allocation semantics remain unchanged.
    No external source request, provider mutation, credential/account action, registration, payment, Calendar
    mutation, notification delivery, Temporal production action, or commit was made.
  - **Verification:** catalog unit coverage includes the two new regressions (**38 passed**); the full required bar
    (`make install`, `make up`, `make migrate`, `make test`, `make test-integration`, `make lint typecheck`,
    `make quality`, and `make quality-load`) is green with `0099 (head)`.

- **2026-07-18 — P33 COMPLETE: generic catalog pre-fetch lease authority fence.** Migration `0100` adds the
  narrowly granted `fn_has_live_catalog_refresh_lease` capability. It validates bounded inputs, locks only the
  exact refresh-run row, then reads PostgreSQL's `clock_timestamp()` after any lock wait; `ec_app` receives only
  `EXECUTE`, not refresh-ledger row access. `CatalogRefreshService` invokes that projection after its post-Pacer
  source/policy rechecks and immediately before generic adapter dispatch. An observed stale/reclaimed token returns
  recoverable `BUSY` before `fetch`; an unavailable projection safely releases the current run and makes no fetch
  (FR-10.3, NFR-8, ADR-001/003/005).
  - **Recovery evidence:** deterministic regressions revoke authority during Pacer admission and simulate an
    unavailable projection, proving no source/catalog effect in either case. A real `ec_app` PostgreSQL race holds
    the exact run row through database-clock expiry: the blocked stale check returns false without changing the run;
    a fresh claimant rotates the token, rejects the old token, and is accepted. The app-role capability regression
    also proves the function is executable while direct refresh-ledger access remains denied.
  - **Deliberate boundary:** this is an entry fence, not an external transaction: a lease can still change after the
    check transaction commits and before an HTTP adapter begins. It neither adds per-physical-GET fencing nor changes
    Pacer allocation for legacy adapters that may issue multiple internal reads. Those adapters remain within the
    separately documented durable-cursor/source-contract boundary; no source profile, external request, provider
    mutation, credential/account action, registration, payment, Calendar mutation, notification delivery, or commit
    was made.
  - **Verification:** focused catalog units (**40 passed**), `0100 → 0099 → 0100`, and the real PostgreSQL
    lock/expiry/reclaim race pass. `make install`, `make up`, `make migrate`, `make test` (**511 passed,
    197 deselected**), `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across
    **197 source files**), `make quality` (**1 passed**), and `make quality-load` (**1 passed, 1 deselected**) all
    pass. `alembic heads` reports `0100 (head)`.

- **2026-07-18 — P34 COMPLETE: P15a transient-lease workflow recovery.** `CatalogRefreshWorkflow` now treats a
  `BUSY` result exactly as the paged catalog spine does: it records a one-second Temporal timer and reinvokes the
  same single-GET activity with the unchanged source/run identity. This prevents a transient competing or
  post-Pacer-expired lease from completing the deterministic P15a workflow successfully before its durable catalog
  run has succeeded. The one-attempt activity policy remains intact, so Temporal never blindly retries a source GET
  (FR-10.3/10.4, NFR-8, ADR-003/005).
  - **Recovery evidence:** a time-skipping Temporal regression returns `BUSY` once and then `SUCCEEDED`; one
    workflow handle reaches the eventual success and both observed activity calls carry the exact original source,
    run key, and single-GET fence. It fails under the former terminal-`BUSY` behavior.
  - **Deliberate boundary:** the one-second durable retry matches the existing paged workflow and holds no worker
    slot, but it is not a new general backoff policy. This slice changes neither source profiles, Pacer allocation,
    activity retry policy, provider behavior, nor historical closed workflow executions; no source egress, provider
    mutation, credential/account action, registration, payment, Calendar mutation, notification delivery, or commit
    was made.
  - **Verification:** focused Temporal workflow coverage (**8 passed**) and the full required bar (`make install`,
    `make up`, `make migrate`, `make test`, `make test-integration`, `make lint typecheck`, `make quality`, and
    `make quality-load`) are green with `0100 (head)`.

- **2026-07-18 — P35 COMPLETE: watch-poll detector-entry lease fence.** Migration `0101` gives `ec_app` a
  narrow `fn_has_live_watch_poll_lease` projection while retaining no raw access to the public cursor. The capability
  validates the exact public watch/token, locks that one `watch_poll_state` row, and reads PostgreSQL's
  `clock_timestamp()` after any lock wait. `WatchPollScheduler` calls it immediately before its fixture detector;
  an observed expired or reclaimed lease increments `lost_leases` and leaves the cursor untouched, with no detector
  call, normalized observation, change-ledger write, or cursor acknowledgement (FR-8.7a, NFR-8/NFR-17, ADR-008).
  The in-memory control-plane double now uses an injected clock and exact-token/expiry check for equivalent offline
  coverage.
  - **Recovery evidence:** an offline regression revokes the first pre-poll lease, proves zero detector and ledger
    effects, then lets the next claimant poll and create one durable change/delivery. A real `ec_app` PostgreSQL
    race holds the exact public cursor through database-clock expiry; its blocked lease check returns false without
    changing the cursor, while a fresh claim rotates the token, rejects the old token, and is accepted. The app-role
    capability test proves only the fifth fixed function is newly executable and malformed input returns false.
  - **Deliberate boundary:** this fences only a lease already stale/reclaimed when the entry check runs. Its database
    transaction ends before a future detector/provider call, so it is not an atomic external-poll fence or a source
    Pacer/cadence/quota design. The scheduler and detector remain fixture-only; no real source poll, provider
    request, source activation, credential/account action, registration, payment, Calendar mutation, notification
    delivery, Temporal production action, or commit was made.
  - **Verification:** focused watch-poll units (**10 passed**), focused PostgreSQL module (**5 passed**), and the
    `0101 → 0100 → 0101` function-only migration round trip pass. `make install`, `make up`, `make migrate`,
    `make test`, `make test-integration`, `make lint typecheck`, `make quality`, and `make quality-load` are green
    with `0101 (head)`.

- **2026-07-18 — P36 COMPLETE: closed-workflow calendar-repair post-effect ACK-loss proof.** The ADR-008 repair
  worker already reconciles before it attempts `mark_repaired`, and it already short-circuits when the durable
  organizer-change marker exists. This slice adds the missing two-pass regression: the first lease commits the
  normal guarded reconciliation effect but loses its queue acknowledgement; a replacement lease with a distinct
  token and incremented attempt count sees `organizer_change_applied=True` and only acknowledges the repair. It
  proves one reconciliation/calendar effect across the crash/reclaim boundary (ADR-007/ADR-008, NFR-8).
  - **Recovery evidence:** the test records the first marker check as false and the reclaimed check as true, asserts
    the first `mark_repaired` rejection, then verifies the fresh acknowledgement succeeds with exactly one
    reconciliation invocation and no retry. The test double models a rejected exact-lease ACK, not a new production
    queue contract; reconciliation/CalendarPort behavior remains covered at its existing boundary.
  - **Deliberate boundary:** no application or adapter production behavior, schema, capability, Calendar contract,
    marker tri-state semantics, provider integration, source poll, credential/account action, notification delivery,
    Temporal production action, or commit changed. In particular, this does not reinterpret
    `organizer_change_applied=False`, whose finer tri-state repair contract remains a separately deferred owner
    decision.
  - **Verification:** focused calendar-repair units (**5 passed**); `make install`, `make up`, `make migrate`,
    `make test`, `make test-integration`, `make lint typecheck`, `make quality`, and `make quality-load` are green
    with `0101 (head)`.

- **2026-07-18 — P37 COMPLETE: replay-safe serial organizer-signal deduplication.** A retained
  `RegistrationWorkflow` now records the versioned Temporal marker
  `p37-organizer-change-serial-dedup-v1` before it can open its organizer-change buffer. New marked
  executions retain handled fingerprints in workflow state, reject a later redelivery of an already-completed
  fingerprint, and still coalesce concurrent duplicates in the existing pending map. The fingerprint becomes
  handled only after `reconcile_organizer_change` returns, so an activity crash remains retryable with its same
  workflow-minted transition identity (FR-8.7/8.7a, NFR-8, ADR-003/ADR-008).
  - **Recovery evidence:** a real time-skipping Temporal/PostgreSQL regression waits on the typed workflow query
    until the first reschedule has been consumed, disables the worker workflow cache so every later task replays
    from history, then delivers that exact fingerprint before a distinct cancellation. Only the reschedule and
    cancellation activities run; the mock CalendarPort sees its initial schedule plus one reschedule upsert, the
    deterministic entry is then removed, and the guarded outbox has exactly one `lifecycle.reconciled` effect.
    The old pending-only behavior would run a second reschedule/upsert before the database ledger suppressed its
    duplicate notification.
  - **Replay boundary:** histories that predate the marker replay their historical command sequence unchanged;
    newly started retained children record and enforce the dedup rule. Inputs that are not retained do not emit the
    marker because they never accept organizer-change traffic; a retained child may record it before a later
    pre-booking failure/handoff. No schema, provider, source poll, credential/account action, registration
    mutation, Calendar contract, notification delivery, or commit changed.
  - **Verification:** focused Temporal regression (**1 passed**), complete workflow integration module, `make
    install`, `make up`, `make migrate`, `make test` (**513 passed, 200 deselected**), `make test-integration`,
    `make lint typecheck` (Ruff clean; strict mypy clean across **197 source files**), `make quality`, and
    `make quality-load` are green; `alembic heads` remains `0101 (head)`.

- **2026-07-18 — P38 COMPLETE: calendar-repair pre-reconcile lease authority fence.** Migration `0102` adds the
  narrow `fn_has_live_calendar_repair_lease` capability. It validates bounded inputs, locks only the exact opaque
  repair row, then checks its repaired state, exact token, and PostgreSQL `clock_timestamp()` expiry after any
  lock wait; `ec_app` has `EXECUTE` only and still has no raw repair-queue access. After the existing
  `organizer_change_applied` marker check is false and the canonical event is available,
  `ClosedWorkflowCalendarRepairWorker` runs this projection immediately before the direct reconciliation path.
  An observed stale/reclaimed lease becomes `lost_leases` with no reconcile, CalendarPort effect, queue ACK, or
  retry under the stale token (FR-8.7/8.7a, NFR-8, ADR-008).
  - **Recovery evidence:** the offline two-pass regression revokes authority at that final check and proves zero
    reconciliation, acknowledgement, or retry before a distinct fresh lease performs exactly one reconciliation
    and ACK. A real `ec_app` PostgreSQL race locks the repair row through database-clock expiry; the blocked stale
    check returns false without changing the row, then a fresh claim rotates its token and is authorized. The
    capability is executable under the non-superuser app role, while malformed and oversized inputs fail closed;
    `0102 → 0101 → 0102` passes.
  - **Deliberate boundary:** the existing `organizer_change_applied=False` marker contract remains untouched; it
    is not reinterpreted as lease authority. This is an observed final entry fence, not an external transaction:
    its database check still ends before a future reconciliation/calendar call, so the final check-to-effect race
    remains explicit. No Calendar upsert policy, marker tri-state, provider, source poll, credential/account,
    registration, notification, or commit behavior changed.
  - **Verification:** focused calendar-repair units (**6 passed**), focused PostgreSQL lock/expiry regression
    (**1 passed**), complete adjacent change-detection/workflow modules, and `make install`, `make up`, `make
    migrate`, `make test` (**514 passed, 201 deselected**), `make test-integration`, `make lint typecheck` (Ruff
    clean; strict mypy clean across **197 source files**), `make quality`, and `make quality-load` are green;
    `alembic heads` reports `0102 (head)`.

- **2026-07-18 — P39 COMPLETE: request-start stale post-effect ACK-lease recovery proof.** The existing
  ADR-003 start relay deliberately leaves a row recoverable when `TemporalRequestWorkflowStarter.start()` has
  returned but `mark_start_started()` rejects the relay's no-longer-current exact lease. This slice closes the
  missing composition proof without changing production behavior: a real PostgreSQL/Temporal regression starts
  the deterministic parent, then expires only that held lease before its durable acknowledgement. The next exact
  claim receives a rotated token, replays the same parent identity through Temporal's normalized
  `REJECT_DUPLICATE` path, and atomically marks the request/outbox started (FR-6.8, AC-48, NFR-8, ADR-003).
  - **Recovery evidence:** the first exact-request relay attempt returns not-started with no retry scheduling,
    leaves the request `received` and its outbox row unstarted/expired, and has one visible parent identity. The
    fresh claim then succeeds with the same tenant/request pair, distinct token, no second parent effect, and
    final `started` request/outbox state. A companion offline two-lease regression asserts the relay's stats and
    proves it never reschedules after an acknowledged engine effect whose database ACK lease is stale. The relay
    now correctly counts that rejected acknowledgement as `lost_leases`, not `retried`: no backoff write occurred,
    and a later fresh claim owns recovery.
  - **Terminology boundary:** this models a rejected stale exact-lease acknowledgement, not a database commit
    whose client response was lost. In the latter case the durable row is already started and no reclaim occurs;
    it is a separate transport-observability concern, not silently treated as this recovery branch. No schema,
    provider, source, credential/account, workflow, Calendar, notification, or commit behavior changed.
  - **Verification:** focused request-start units (**3 passed**) and complete PostgreSQL/Temporal request-start
    module (**6 passed**); `make install`, `make up`, `make migrate`, `make test` (**515 passed, 202
    deselected**), `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **197
    source files**), `make quality`, and `make quality-load` are green with `0102 (head)`.

- **2026-07-18 — P40 COMPLETE: event-change fanout pre-signal lease authority fence.** Migration `0103` adds
  the narrow `fn_has_live_event_change_delivery_lease` capability. It validates the exact opaque delivery
  identity/token, locks only that `event_change_deliveries` row, then checks delivered state, token, and
  PostgreSQL `clock_timestamp()` expiry after any lock wait; `ec_app` has `EXECUTE` only and retains no raw
  fanout-queue access. `ChangeDetectionService` invokes it immediately before the Temporal organizer-change
  signal. An observed stale/reclaimed lease increments `lost_leases` and performs no signal, acknowledgement,
  release, or closed-workflow calendar-repair enqueue under its old token (FR-8.7/8.9, NFR-8, ADR-008).
  - **Recovery evidence:** an injected-clock offline two-pass regression revokes the first lease at that final
    check, proves zero signal/ACK/retry/repair effect, then lets a fresh second-attempt lease deliver exactly once.
    A real `ec_app` PostgreSQL row-lock race holds the delivery through database-clock expiry: the blocked check
    returns false without changing delivery state; a fresh claim rotates its token, increments its attempt, and is
    accepted. The app-role capability regression confirms the new fixed function is executable while direct
    control-plane access stays denied; malformed and oversized tokens fail closed. `0103 → 0102 → 0103` passes.
  - **Deliberate boundary:** this is an observed final entry fence, not an atomic Temporal transaction. Its check
    ends before `signal_organizer_change`, so the residual check-to-signal/ACK race remains. P37's retained-child
    serial fingerprint dedup makes a later same-fingerprint redelivery safe after that residual race; it does not
    turn Temporal signaling into exactly-once delivery. P22 terminal fencing and closed-workflow repair behavior
    are unchanged. No Pacer behavior, source/provider request, credential/account action, registration mutation,
    Calendar mutation, notification delivery, or commit was made.
  - **Verification:** focused fanout units (**9 passed**) and the real PostgreSQL lock/expiry regression
    (**1 passed**) pass. `make install`, `make up`, `make migrate`, `make test` (**516 passed, 203 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **197 source files**),
    `make quality` (**1 passed**), and `make quality-load` (**1 passed, 1 deselected**) are green; `alembic heads`
    reports `0103 (head)`.

- **2026-07-18 — P41 COMPLETE: watch-projection pre-registry lease authority fence.** Migration `0104` adds the
  narrow `fn_has_live_watch_projection_lease` capability. It validates the exact projection/token, locks only its
  opaque lifecycle-watch outbox row, then checks delivery state, token, and PostgreSQL `clock_timestamp()` expiry
  after any wait; `ec_app` receives `EXECUTE` only and retains no raw projection-outbox access. The projection relay
  calls it immediately before the tenant-scoped watch registry `register`/`unregister` operation. An observed
  stale/reclaimed lease increments `lost_leases`, with no registry call, acknowledgement, or reschedule under the
  old token (FR-8.7a, NFR-8, ADR-008).
  - **Recovery evidence:** an offline two-pass regression revokes the first final lease and proves zero registry,
    acknowledgement, and retry effect before a distinct fresh lease registers and acknowledges once. Its companion
    regression proves a registry/capability error whose terminal reschedule loses authority is counted as
    `lost_leases`, not a false retry. A real `ec_app` PostgreSQL race locks the exact outbox row through
    database-clock expiry: the blocked check leaves it unchanged; a fresh claim rotates its token, increments the
    attempt, and is authorized. Malformed and oversized inputs fail closed, and `0104 → 0103 → 0104` passes.
  - **Deliberate boundary:** this is an observed final entry fence, not an atomic registry transaction. Its check
    ends before `register`/`unregister`, so the residual check-to-registry-call race remains; existing lifecycle-
    locked, idempotent registry functions and durable projection recovery remain load-bearing. It does not infer
    lifecycle staleness from a false queue lease or change P21's terminal capability contract. No Pacer/source or
    provider request, credential/account action, registration mutation policy, Calendar mutation, notification
    delivery, Temporal production action, or commit was made.
  - **Verification:** focused projection units (**4 passed**) and the real PostgreSQL lock/expiry regression
    (**1 passed**) pass. `make install`, `make up`, `make migrate`, `make test` (**519 passed, 204 deselected**),
    `make test-integration`, `make lint typecheck` (Ruff clean; strict mypy clean across **197 source files**),
    `make quality` (**1 passed**), and `make quality-load` (**1 passed, 1 deselected**) are green; `alembic heads`
    reports `0104 (head)`.

- **2026-07-18 — P42 COMPLETE: request-start pre-Temporal lease authority fence.** `RequestStartRelay` now
  checks its exact start-outbox lease immediately before it asks Temporal to create the deterministic parent. A
  false result increments `lost_leases` and makes no Temporal call, durable acknowledgement, or retry write under
  the stale token (FR-6.8, NFR-8, ADR-003). No migration is intentional: unlike P40/P41's raw-access-revoked
  queues, `request_start_outbox` is ADR-003's pre-existing opaque direct-DML global queue, on which `ec_app`
  already claims, acknowledges, retries, and reads through `system_session_scope`.
  - **Authority check and accounting:** the PostgreSQL adapter first locks exactly `(request_id, tenant_id)`, then
    checks started state/token/expiry and issues a separate `clock_timestamp()` comparison only after the lock
    wait. That adds no table authority or data exposure. `lost_leases` deliberately aggregates an observed stale
    pre-Temporal entry lease, a P39 post-start acknowledgement rejection, and a failed terminal retry write; none
    of those represents a scheduled backoff retry. A pure reclaim rotates the token but preserves `attempt_count`.
  - **Recovery evidence:** an offline two-pass regression proves the first stale lease produces zero Temporal,
    ACK, or retry effect, while its fresh token starts and acknowledges once. A companion regression proves an
    effectful Temporal error whose terminal retry has lost authority is not misreported as `retried`. A real
    `ec_app` PostgreSQL row-lock race blocks the adapter check through database-clock expiry, leaves every mutable
    queue fact unchanged, then lets a fresh token reclaim; stale token and wrong-tenant probes fail closed while
    the fresh exact token succeeds. The complete request-start PostgreSQL/Temporal module remains green.
  - **Deliberate boundary:** the lock/check transaction ends before Temporal egress, so the residual
    check-to-start race remains. Likewise P39's post-start ACK race remains recoverable through the deterministic
    parent identity and Temporal `REJECT_DUPLICATE`; neither is represented as exactly-once start delivery. No
    schema/privilege change, provider/source request, credential/account action, Calendar mutation, notification
    delivery, or commit was made.
  - **Verification:** focused request-start units (**5 passed**), focused PostgreSQL race (**1 passed**), and the
    complete request-start PostgreSQL/Temporal module (**7 passed**) pass. `make install`, `make up`, `make
    migrate`, `make test` (**521 passed, 205 deselected**), `make test-integration`, `make lint typecheck` (Ruff
    clean; strict mypy clean across **197 source files**), `make quality` (**1 passed**), and `make quality-load`
    (**1 passed, 1 deselected**) are green; `alembic heads` remains `0104 (head)`.

- **2026-07-18 — P43 COMPLETE: recovered repository, fail-closed production foundation, and secure handoff
  completion (production launch not claimed).** The recovered working tree is preserved in local root commit
  `f947698`; no private upstream could be identified, so that root must be reconciled with the owner-approved
  remote before any push. Non-mock composition now requires a deployment-owned runtime provider plus explicit
  OIDC, object-store, notification, vault, Calendar, and provider-operation ports. Signed asymmetric JWT/JWKS
  validation, bounded single-flight key refresh, version-aware convergent S3 purge, lazy API Temporal recovery,
  bounded dependency readiness, locked non-root packaging, pinned CI actions, and an operations runbook all fail
  closed at their authority boundaries.
  - **Handoff completion:** migration `0105` stores only a SHA-256 digest of each 256-bit capability and an exact
    replay receipt. Scanner-safe GET is inert; only POST signals the workflow. Verified completion re-reads
    provider truth and fresh Calendar availability before one atomic `HANDOFF → REGISTERED` transition, task
    completion, expiry retirement, and receipt. Review-required self-reports instead consume the capability,
    record durable evidence, notify the user, and leave Calendar/lifecycle truth unchanged. Privileged receipt
    replay is explicitly tenant-bound, including a direct cross-tenant `SECURITY DEFINER` regression, and exact
    receipts are replayed before catalog/provider reads after an activity acknowledgement loss. Capability paths
    are excluded from access logs.
  - **Recovery and availability evidence:** the final audit closed unknown-key JWKS fetch amplification,
    malformed issuer/completion URLs, versioned S3 delete-marker leakage, unbounded database readiness, Temporal
    cold-start coupling, silent review-outbox acknowledgement, and verified/review activity-ACK replay gaps. A
    Docker/Colima clock-skew flake in the pre-existing watch-poll race proof now uses its persisted logical expiry;
    the focused race passes **5/5** and its full module passes **5/5**.
  - **Verification:** Ruff is clean; strict mypy is clean across **203 source files**; unit tests are **631 passed,
    208 deselected**; integration tests are **207 passed, 632 deselected**; the synthetic quality matrix is **1
    passed** and bounded quality load is **1 passed, 1 deselected**. `0105 → 0104 → 0105` passes. The exact
    production image runs as UID/GID **10001**, imports the installed package, reports `/healthz` healthy, and
    reports PostgreSQL plus Temporal ready.
  - **Explicit remaining boundary:** production still requires an owner-approved remote/history reconciliation,
    deployment credentials and real runtime providers, authenticated operator review/approve-reject ownership,
    P20 Calendar tri-state/upsert policy, P24a notification post-send ACK ownership, draft-v0.3 FR11–18
    implementation or signed deferral, full account-erasure retention/legal-hold/tombstone/workflow fencing,
    realistic G1 corpus, Meetup Pro G2, relay-domain G3, Temporal O-6, and real domain/legal/managed-infrastructure,
    restore, observability, and on-call evidence. Mutable local Compose dependency tags remain an external
    supply-chain review item; none of these gates is represented as complete.

- **2026-07-19 — P44 COMPLETE: bounded ingress/Temporal availability, deterministic catalog concurrency,
  protected notification capabilities, and receipt-aware handoff recovery (production launch not claimed).**
  Catalog ingestion now resolves exact source identity before fuzzy matching, serializes overlapping fuzzy
  domains with deterministic advisory locks, and pre-locks all existing canonical winners in UUID order. Repeated
  persistent-slice runs page to their exact ranked candidates rather than assuming a clean catalog. API ingress
  enforces a 64 KiB decoded-body limit plus a whole-body deadline, skips buffering for safe bodyless methods,
  accepts only bounded canonical decimal feed cursors, and omits the mock onboarding route outside mock mode.
  Temporal eager connect and every API/worker start, signal, and describe RPC now have validated 0.1–60 second
  deadlines.
  - **Capability and delivery boundary:** migration `0106` prohibits plaintext completion URLs in the global
    notification outbox. Registration stores tenant-bound AES-256-GCM ciphertext through a deployment-supplied
    protector; the relay claims both queue and notification authority before decrypting, never persists untrusted
    exception text, quarantines unknown/insecure projections, and scrubs ciphertext after delivery or terminal
    failure. The migration safely quarantines pending legacy plaintext, retires stale undelivered ledger entries,
    preserves delivered ledgers without their secret, validates the new constraint, and never reconstructs
    plaintext on downgrade. Non-mock composition fails closed when no protector is supplied.
  - **Handoff recovery boundary:** provider, Calendar, and Pacer availability failures remain retryable workflow
    control while policy/consent/reconsent failures become durable review outcomes. Completion activities use one
    attempt per workflow-owned cycle; retry floors grow `1m → 2m → 4m` to a one-hour cap and remain
    command-interruptible. Organizer and authorized un-RSVP signals are buffered before the activity
    acknowledgement gap. If verification commits but its acknowledgement is lost at TTL, expiry observes the
    completed task and returns `completion_committed`; the child prioritizes exact receipt replay and can no
    longer overwrite `REGISTERED` with `EXPIRED`. A persistent pre-commit dependency outage still expires
    normally.
  - **Verification:** Ruff is clean; strict mypy is clean across **205 source files**; all collected tests were
    exercised as **660 unit passed**, **221 integration passed** (excluding the separately executed load marker),
    and **1 quality-load passed**; the synthetic quality matrix also passes. The complete Temporal workflow module
    is **36 passed**, including lost-ACK, TTL-edge, signal-buffer, and exponential-backoff histories. The
    persistent-catalog vertical slice passes twice. Real PostgreSQL `0106 → 0105 → 0106` passes with `0106
    (head)` restored. The locked image rebuilds and runs read-only with `no-new-privileges` as UID/GID **10001**,
    imports the installed package without development tools or `.env`, reports `/healthz` healthy, and reports
    PostgreSQL plus Temporal ready. Offline lock verification, Compose configuration validation, diff checks, a
    repository credential-pattern scan, and an independent blocker-only review are clean.
  - **Explicit remaining boundary:** P43's production gates remain open: owner-approved remote/history
    reconciliation, deployment credentials and real runtime/provider/protector implementations, authenticated
    operator review ownership, remaining Calendar/notification/account-erasure and draft-v0.3 scope, realistic
    quality corpora, managed infrastructure/restore/observability/on-call evidence, and external supply-chain
    review. These improvements harden and verify the local implementation; they do not claim production launch.

- **2026-07-20 — P45 COMPLETE: bounded durable-start recovery and disposable integration state (production
  launch not claimed).** Live recovery traced a visible ten-second workflow-task timeout to a local saturation
  burst, not a deterministic workflow defect. Service tests had populated the persistent `ec` database; the
  request-start relay then drained 156 due parents without a busy-cycle pause, amplifying them into hundreds of
  registration children. The combined Temporal worker inherited broad SDK defaults while sharing a small
  SQLAlchemy pool, producing hundreds of threads, pool checkout timeouts, late task completions, and deadlock
  warnings. Only the workflow worker and request-start relay were paused during diagnosis; PostgreSQL, Temporal
  history, and accepted work were preserved.
  - **Backpressure and isolation:** the worker now validates and advertises explicit eight-workflow/eight-activity
    defaults, owns an equally bounded workflow executor, and closes that executor after SDK shutdown. The durable
    start relay claims five parents at most once every two seconds even while busy, while preserving lease,
    retry, and reject-duplicate behavior. Integration, quality, bounded-load, and the two-pass vertical-slice
    targets now create, migrate, and finally drop one exact UUID-named `ec_test_*` database; direct service tests
    pointed at `ec` fail before fixtures run. URL guards reject libpq query options that could override
    database/host/user/service identity. A fresh-database quality failure was corrected with a fixture-local
    discovery policy; production's `public_jsonld` policy remains fail closed. An API identity test now uses its
    recording starter instead of leaving an unserved workflow on the persistent Temporal test queue.
  - **Read-path and scanner correctness:** the nightly invariant scan now spaces Temporal describes at a validated
    default ten calls per second and stops issuing liveness RPCs after the first uncertain response while still
    counting the complete PostgreSQL inventory as uninspectable. A recovery canary also exposed three independent
    recommendation defects: singular `free technology event` wording was not an explicit price constraint,
    elapsed durable catalog rows could re-enter an unbounded time window, and rehydrated workflow requests lost
    the semantic embedding used by the API. The parser now recognizes only direct/known-category free-event noun
    phrases (with availability and `gluten-free` negative guards), every catalog retrieval requires
    `start_at >= CURRENT_TIMESTAMP`, and the discovery activity reconstructs the omitted intent vector without
    putting request text or embeddings in Temporal history.
  - **Live recovery evidence:** after a state-preserving Temporal server restart, the bounded worker started with
    8/8 slots at roughly 99 MiB and single-digit process/thread count rather than hundreds. Three confirmed inert
    test-queue workflows were terminated by exact workflow/run ID. The first recovery canary's 69-event parent
    completed in **0.992 seconds** without a workflow/activity timeout or failure. After the recommendation fixes,
    a second real API canary ran concurrently with the paced historical scan: it persisted `budget_free=true`,
    returned only verified-free future rows, and the durable workflow attempted the API's exact top three IDs.
    Its 68-event parent completed in **0.696 seconds** with no failed/timed-out history event; the retained handoff
    remained correctly open with a future seven-day TTL rather than expiring immediately. The complete paced scan
    finished in **14m40s** with **8,628 inspected, 1 open, 8,627 closed/missing, and 0 uninspectable**; one
    watch-unregister projection visible in its start-of-scan snapshot had converged to zero on the current snapshot.
    The API stayed ready, worker memory stayed near 102 MiB, and one isolated CLI health deadline did not reproduce
    across nineteen immediate/spaced retries (`SERVING`). Live logs contain none of the prior `QueuePool`,
    `Task not found`, `TMPRL1101`, deadlock, or workflow-task-timeout signatures.
  - **Verification:** `make install`, `make up`, `make migrate`, `make lint`, `make typecheck`, `make test-unit`,
    `make test-integration`, `make quality`, `make quality-load`, `make slice`, and `make build` pass. Final counts
    are **706 unit passed** and **222 integration passed**; the quality matrix is **1 passed**, bounded quality
    load is **1 passed, 1 deselected**, Ruff is clean, and strict mypy is clean across **205 source files**. Every
    disposable database was removed, the final integration run created no persistent Temporal workflow, and the
    runtime request/start/lifecycle aggregates were unchanged across isolated tests and the isolated slice.
  - **Explicit remaining boundary:** forty-one pre-isolation fixture starts remain deliberately preserved in the
    local runtime database, all marked with the integration retry fixture and scheduled for 2099, so the relay
    correctly ignores them. A read-only provenance audit classified the scanner's pre-canary **8,664**
    nonterminal lifecycle rows and 477 malformed identities as historical fixture-shaped residue rather than a
    new live transition defect; no row-level deletion is safe without a complete dependent-table purge. Removing
    broader historical local-test data is therefore a separate explicit reset/retention choice. P43/P44 production
    gates remain open; this local recovery does not establish managed capacity, observability, restore, external
    provider, or production-launch evidence.

- **2026-07-20 — P46 COMPLETE: same-origin consumer web MVP and tenant-safe product projections (public
  production launch not claimed).** The API image now serves a responsive consumer product at `/` and `/app`,
  with local-demo onboarding, focused catalog previews, durable concierge requests, recent briefs, lifecycle-backed
  Plans, actionable handoffs, preference editing, feedback, and withdrawal. Preview and durable modes are visibly
  distinct; an accepted brief is never presented as a completed registration. The plain HTML/CSS/JavaScript client
  is packaged with the Python application, uses no unsafe HTML sink, validates external destinations, restores
  keyboard focus across dynamic transitions, honors reduced motion, exposes high-contrast focus states, and follows
  Plans/To-do cursors to an explicit 200-item display bound rather than silently dropping later pages.
  - **Consumer truth and authority boundary:** new RLS-backed read models expose only account, request, event,
    lifecycle, and handoff facts needed by the product. They omit completion capabilities and Temporal identities,
    explicitly predicate every tenant query, hide internal `failed_no_candidate` attempts, keep selected-provider
    labels and URLs consistent, and exclude expired handoffs from actionable results. Authenticated completion is
    tenant-bound and returns `410` after absolute TTL. Every authenticated route now verifies that the resolved
    tenant is provisioned before any read or write, preference revisions conflict with `409`, browser-ambiguous auth
    redirects are rejected, and consumer command responses no longer disclose workflow IDs. Production composition
    still requires a deployment-owned session/BFF and runtime provider graph; local header auth and onboarding exist
    only when `EC_MOCK_CLOUD=true`.
  - **Live product evidence:** the locked image was rebuilt and the complete Compose application profile recreated;
    API and all durable workers report healthy, while `/readyz` reports PostgreSQL and Temporal ready. A real Chrome
    acceptance pass exercised the authenticated desktop and 390 px mobile layouts, preview pagination, Plans,
    To do, Settings, safe external links, and the accessibility tree with zero page console/runtime errors or
    horizontal overflow. The retained durable UI canary completed its 68-event parent in about **1.21 seconds** and
    projects one truthful handoff without exposing its two internal failed candidate attempts. A post-rebuild log
    scan found no traceback, error, workflow-task timeout, deadlock, pool exhaustion, nonzero retry, or lost-lease
    signature.
  - **Verification:** Ruff is clean; strict mypy is clean across **209 source files**; unit tests are **723 passed,
    227 deselected**; integration tests are **226 passed, 724 deselected** in a disposable database. The synthetic
    quality matrix is **1 passed**, bounded quality load is **1 passed, 1 deselected**, and the disposable Temporal
    slice is **1 passed**. JavaScript syntax, DOM/ARIA reference checks, diff/format checks, security headers, static
    asset serving, the locked non-root image build, and the final healthy stack all pass.
  - **Explicit remaining boundary:** public launch still needs the deployment-owned OIDC/BFF session and CSRF policy,
    real provider/Calendar/notification/secret implementations and credentials, managed infrastructure and release
    evidence, and the broader P43-P45 account-erasure/quality/operations gates. Product follow-ons are a durable
    request-to-outcome projection, push/poll lifecycle refresh instead of manual refresh, and a committed browser E2E
    job (all three repository follow-ons are implemented by P47 below). Historical fixture-shaped catalog/runtime
    rows remain deliberately preserved; cleaning them is a separate owner-approved reset/retention action, not part
    of this UI build.

- **2026-07-22 — P47 COMPLETE: selected-request truth, bounded browser freshness, and repository
  product gates (public production launch not claimed).** Migration `0107` adds the insert-only,
  RLS-protected `request_outcome_links` projection with tenant-consistent request/lifecycle foreign
  keys. After a child commits the selected registration or handoff outcome, the patched parent
  workflow records that stable lifecycle through a replay-convergent activity; a different-lifecycle
  rebind is non-retryable, failed internal attempts stay hidden, and legacy/unselected requests remain
  honestly outcome-free. Recent briefs now render the linked lifecycle's live state without querying
  Temporal or inferring from candidate order.
  - **Browser freshness and security boundary:** while visible and signed in, one timer chain watches
    only recent unresolved requests at a jittered 30-second base cadence, backs off toward five
    minutes after failures, and refreshes Plans/To do once when request truth changes. It stops when
    hidden, signed out, aged out, or resolved; per-resource generations prevent stale-load overwrite,
    and transient background failures retain last-known truth. Every authenticated consumer mutation
    now passes through `CsrfProtectionPort` after tenant
    authentication/provisioning; non-mock composition fails closed without an injected verifier,
    while the no-op remains paired only with explicit local header auth.
  - **Committed product gate:** locked Python Playwright dependencies, `make browser-install`,
    `make test-browser`, and a dedicated GitHub Actions Chromium job exercise the real packaged
    FastAPI shell with deterministic product API fixtures across local onboarding, preview versus
    durable semantics, selected outcomes, Plans, handoffs, Settings, accessibility/error behavior,
    security headers, and desktop/mobile overflow. Focused unit and PostgreSQL/Temporal integration
    coverage for the projection, replay fence, refresh scheduler, and CSRF route matrix is committed.
  - **Final verification and live local evidence:** locked dependency sync passes; Ruff is clean; strict
    mypy is clean across **209 source files**; unit tests are **741 passed, 235 deselected**; and the
    fresh-database integration suite is **229 passed, 747 deselected**. The synthetic quality matrix is
    **1 passed**, bounded five-repeat quality load is **1 passed, 1 deselected**, the disposable vertical
    slice is **1 passed**, and the Chromium product suite is **5 passed**. Migration `0107` also passed a
    deliberate interrupted-build rehearsal: a healthy partial index was reused, a stale same-name index
    was rebuilt, and downgrade/re-upgrade converged. The locked non-root image builds; `0107` is applied to
    the preserved local database; API plus all durable workers were recreated from that image and are
    healthy; `/healthz`, `/readyz`, the packaged consumer shell, and security headers return `200`; and the
    post-recreate application-log scan contains no traceback, exception, error, panic, or fatal signature.
  - **Explicit remaining boundary:** the repository supplies the required CSRF port and browser gate,
    not a real identity provider. Public launch still requires deployment-owned OIDC/BFF session and
    CSRF implementations plus production canary evidence, full account erasure, P20/operator
    decisions, real Calendar/notification/secret/source providers and credentials, managed
    infrastructure/observability/restore proof, G1–G3 and legal evidence, owner sign-off, and a
    controlled canary/rollback release.

- **2026-07-22 — P48 COMPLETE: built-in consumer identity, durable account erasure, shared
  external-effect fencing, and production operations foundation (public production launch not
  claimed).** Non-mock composition now includes a same-origin OIDC BFF rather than depending on an
  unspecified deployment adapter. Authorization Code + PKCE sessions are Redis-backed and bounded,
  bind state/nonce/current tenant and subject, enforce tenant-wide permanent revocation, and use the
  provider's `auth_time` plus a purpose-bound `prompt=login` step-up to establish recent
  authentication without extending the original session lifetime. The consumer Settings UI exposes a
  deliberate account-erasure flow: the user must reauthenticate when required and enter the exact
  confirmation phrase `DELETE MY ACCOUNT`; accepted work returns a stable receipt and the browser
  clears its session instead of polling a now-revoked account.
  - **Durable erasure and race boundary:** migration `0108` introduces the permanent pseudonymous
    tenant tombstone, an advisory-lock write fence over tenant-owned tables, immutable workflow and
    Calendar cleanup inventories, begin-time audit PII shredding, and a cross-tenant leased/resumable
    erasure coordinator. Its ordered stages drain admitted external effects; cancel Temporal parents
    before descendants and remove their addressability; sweep deterministic Calendar IDs plus
    owner-marked, paginated tenant events; revoke/delete vault credentials, tenant object prefixes,
    and browser sessions; then verify the retained-audit shred invariant and purge tenant database
    data. Registration, withdrawal, Calendar mutations, claim-check object writes, Temporal parent
    starts, and notification sends now enter one shared PostgreSQL
    `TenantEffectAuthority` using the same tenant advisory lock as erasure start. Already-admitted
    async and thread-backed effects are cancellation-drained before that lock can be released, exact
    queue/ledger authority is re-read inside the fence, and nested claim-check work is reentrant only
    for the same authority instance, tenant, and effect mode.
  - **Operations foundation:** production configuration has a fail-closed example and preflight
    validator; the service exposes release identity at `/versionz` and operational metrics at
    `/metrics`; canary checks, locked-image smoke tests, migration/rollback guidance, and an all-table
    plus sequence restore drill are wired into documented Make/CI gates.
  - **Final repository evidence:** locked dependency installation passes; Ruff is clean; strict mypy
    is clean across **232 source files**; unit tests are **942 passed, 246 deselected**; operations
    contracts are **114 passed**; the Chromium product suite is **6 passed**; and the disposable full
    integration suite is **239 passed, 949 deselected**. The synthetic quality matrix is **1 passed**,
    its configured five-repeat load gate is **1 passed, 1 deselected**, the disposable vertical slice
    is **1 passed**, and the structural production-example validator is **20/20**. Focused evidence
    includes the account-erasure module (**4/4**), cross-boundary security integration (**10/10**),
    combined real-database authority/request/erasure races (**15/15**), request-start integration
    (**7/7**), and a live local Temporal cancel/delete/`NOT_FOUND` proof (**1/1**). Fresh install and
    `0108 → 0107 → 0108` migration replay, the locked non-root image build, and final hygiene/security
    reviews are green.
  - **Controlled local cutover evidence:** all API/writer processes were quiesced before a mode-0600
    custom-format backup was validated and checksummed. The preserved runtime database migrated from
    `0107` to `0108`, exposing the three erasure tables, begin/finalize capabilities, and **78** write-
    fence triggers. The isolated restore drill passed **10/10** across **49 tables and 5 sequences**
    and destroyed only its generated database. A sanitized pre/post comparison found identical counts
    across all **45 shared tables** (**248,308 rows** before and after) and identical states for all
    **5 shared sequences**; the only additions were the four expected empty `0108` schema tables, with
    no removed tables. All eight app containers then started from exact image
    `sha256:221fb8c7e950eead0ba23b3e4286ee16a8b7a2462bb4cdba4b3079532d378a47`; the local canary passed
    **34/34**, health and database/Temporal readiness are green, and worker startup/cycle logs show no
    traceback, provider error, retry, or lost lease. Local identity is intentionally `not_configured`
    and release identity is development/null, so none of this is represented as production IdP or
    immutable-release evidence.
  - **Explicit remaining boundary:** the repository can remove Temporal visibility/addressability, but
    Temporal Cloud physical history deletion—and deletion or disablement of every archival/export
    copy—within the erasure deadline still requires vendor/deployment evidence. Launch also requires
    a realistic G1 corpus, Meetup Pro G2 and relay-domain G3, real provider and confidential IdP
    credentials/canaries, managed
    infrastructure/PITR/restore/observability/on-call proof, legal approval of retention, holds,
    pseudonymous tombstones, and retained audit workflow IDs, plus real vault/session/provider
    revocation evidence. Google events created before the owner marker need an approved backfill or
    cleanup procedure; Calendar watch creation remains disabled until a durable
    create/store/stop-channel protocol closes its provider-created/local-store gap. Authenticated
    operator ownership, P20 Calendar policy, P24a notification post-send acknowledgement, remaining
    draft-v0.3 scope, owner sign-off, and a controlled canary/rollback release are likewise still open.

- **2026-08-06 — Entity page reachable from every event, and insights for every entity
  (migration `0152`).** Owner reported clicking a host on the map and getting a bare
  `event entity not found` banner.
  - **Root cause.** The projection read
    `fn_list_retained_catalog_browse_observations_v1(source, NULL, NULL)`, whose unbounded mode uses
    `statement_timestamp()` as the lower bound, so elapsed events were never projected: **0 of 321**
    past events carrying role names had a mention row, against 83% of future ones. The consumer can
    browse a historical date range, so the gap was reachable from the normal product surface — the
    clicked event had started three days earlier.
  - **`fn_refresh_catalog_entity_index_v2`** keeps the live call verbatim and unions one bounded
    trailing-365-day history call; the branches are mutually exclusive by the retained capability's
    own predicates. 365 days keeps an unscoped rebuild inside the capability's 370-day ceiling. The
    0148/0149 quality gate runs after the rebuild, matching the refresh-commit/paged-promotion
    sequencing (a first cut skipped it and was caught by the existing contract test).
    Past-event coverage **0% → 97%**; entities 572 → 1,229, mentions 673 → 1,464.
  - **`fn_list_catalog_entity_events_v2`** adds `is_past` and orders upcoming ascending before past
    descending, so history cannot crowd out upcoming appearances; the page now shows
    "Upcoming appearances" and "Previously".
  - **`fn_get_catalog_entity_insights_v1`** derives cadence, typical attendance, price mix, topics,
    venues, cities, asserting sources, and recurring collaborators from admitted mentions and
    canonical events only — no provider, no enrichment table, no `identity_status` precondition, so
    the ~50% of entities that are name-only `source_scoped` (which the enrichment plane deliberately
    never researches) get real evidence. Cadence uses a trailing-365/leading-90-day window because
    provider start dates run out to 2099.
  - **UI.** A "Catalog activity" card, clickable collaborator chips, per-fact provenance links next
    to every external value, and a 404 that now names the unindexed entity instead of surfacing raw
    API text. Async external crawling was left as designed: `fn_list_catalog_entities_due_for_refresh_v1`
    already sweeps every URL-anchored entity, never-fetched first.
  - **Evidence.** 1,251 unit · 274 integration (fresh migration chain) · 114 operations · 40 Chromium
    e2e · 87+79 web suites; ruff + strict mypy clean over 260 files. Verified against live data:
    the exact host/organizer chips from the report now resolve, and both an enriched entity (Sentry)
    and a name-only one were screenshot-checked in the running stack.
  - **Deployment note:** the migration ran before the image rebuild, so sources refreshed in that
    window re-ran the old live-only projection and dropped their history again. One idempotent
    `fn_refresh_catalog_entity_index_v2(NULL)` + prune after the rebuild restored it. Migrate and
    deploy together, or rebuild once after.

- **2026-08-25 — Calendar range served by one aggregate instead of paging every event
  (migration `0153`).** Owner reported the calendar showing "No events" for today and empty cells
  for most of the month.
  - **Root cause — the data was never missing.** August 2026 holds **7,500** eligible events and
    Aug 25 alone holds 279. The grid renders per-day totals and topic chips only, but derived them
    by paging the whole visible range at 72 events per request and grouping in the browser:
    **105 sequential round-trips** for that month, restarted from zero on every reload because the
    proxy sends `no-store` and `loadCatalog` resets its list on mount. The owner's screenshots were
    a load in progress — days already filled matched the catalog exactly (Aug 16–24 = 147, 222,
    287, 336, 332, 218, 270, 153, 219), and everything past the point it had reached read as empty.
  - **`fn_list_catalog_day_facets_v1`** runs the same unbounded observation/eligibility scan
    `fn_list_catalog_topic_facets_v2` (0151) runs and buckets it into local calendar days. Two
    deliberate differences from that facet: it applies the topic selection, because the grid must
    agree with the agenda its own filters produce; and it takes an IANA `p_time_zone` and buckets on
    `start_at AT TIME ZONE p_time_zone`, because a calendar day is local wall clock and a UTC bucket
    would move every Pacific evening event to the next day. Untopiced events are counted under the
    synthetic `other` bucket, matching the client taxonomy. Day totals are distinct events, so a
    day is not the sum of its topic rows.
  - **`GET /v1/catalog/events/summary`** accepts exactly the filters the paged route accepts, minus
    paging and ordering. Both routes now share one `_normalized_catalog_filters` validator and one
    adapter-side `_validated_catalog_filter_inputs`, so a range can never be summarized under a
    filter the agenda would refuse. The time zone is validated before the query so an invalid zone
    is a 422 rather than a driver error.
  - **Client.** The calendar reads counts for the range and events only for the open day; week
    columns preview four events per day beside the authoritative count. A per-tab, per-tenant
    `sessionStorage` summary cache (counts only, 5-minute TTL, bounded, cleared on session loss)
    paints a reload before revalidation lands. A day window matches by interval overlap, so the
    agenda filters to events that *start* that day — matching what the grid counted — and skips
    bounded pages of still-running earlier events rather than showing an empty agenda beside a cell
    counting hundreds.
  - **Evidence.** 1,366 unit · 277 integration (fresh migration chain) · 40 Chromium e2e · 214 web
    suites; ruff + strict mypy clean over 266 files; production frontend build green. The new
    integration test reads a range page-by-page the way the calendar used to and asserts the
    aggregate reproduces it day-for-day and topic-for-topic. Verified against the running stack with
    Playwright: **August completes in ~1.0s over 3 requests** (was 105), a reload first-paints from
    cache in **0.03s**, all 33 populated days render, Aug 25 reads 279 events with 19 topic chips,
    and the agenda lists only Aug 25.
  - **Also confirmed healthy while diagnosing:** the ingestion cadence wakes every 300s, dispatches
    a bounded batch, and the Redis pacer defers the rest by design (`deferred: 9, succeeded: 1`);
    `luma-sf`, `meetup-nyc`, `meetup-sf`, and `luma-nyc` all refreshed successfully during the
    session.

- **2026-08-25 — Shared Pacer buckets expired between refreshes, so every catalog source was
  deferred before any provider call.** Investigating apparent ingestion starvation.
  - **First, a measurement correction that matters for every future ingestion number.** The
    registry reports 526 enabled sources, but **497 of them are `test-*` fixtures** living in the
    shared development database. The real roster is **29 named enabled sources** on 120/360/720/
    1440-minute cadences, asking for ~139 refreshes/day — not the 12,067/day the raw registry
    implies. Any source, refresh, or freshness figure must filter `source_key NOT LIKE 'test-%'`
    first; the same residue already inflates the failed-run count (`fixture cleanup`, 1,224 rows)
    and the "unreviewed source" count (101 rows, all `test-unreviewed-*`). Measured against the
    real roster, 27 of 29 sources were within ~1.1 intervals of schedule.
  - **Root cause.** `RedisPacer._base_ttl_seconds` returned `max(burst / rate, unavailable_retry)`
    — **two seconds**. Every bucket therefore expired within ~3s of its last use, while sources
    refresh 2 to 24 hours apart. The take script deliberately treats a missing bucket as lost
    state and starts it empty (ADR-005), which is right for a real eviction; with that retention
    it fired on **every ordinary attempt instead**. Each attempt returned `W|0.2s`, and the
    refresh service responded by pausing the durable run before any provider call. Verified
    directly against the running Redis: measured bucket TTL **2,999 ms**, first acquire denied,
    an acquire 0.5s later granted, and the bucket gone after 3.2s.
  - **Who it actually hurt.** Paged multi-branch sources, which need many sequential tokens:
    `san-jose-public-library-events` and `sccld-all-physical-branches-events` had accumulated
    **5,099** and **2,186** paused attempts and fallen **49** and **21** refresh intervals behind,
    against a run history that normally completes in 3–17 attempts. Fleet-wide the symptom was 28
    paused runs all carrying `Pacer wait: shared token bucket is refilling or source backoff is
    active`, and roughly one successful refresh per dispatch pass instead of a full batch of 50.
  - **Fix.** `EC_PACER_STATE_RETENTION_SECONDS` (default **7 days**) floors the bucket TTL. The
    binding requirement is expressed in code, not prose: retention must outlast
    `MAX_SOURCE_REFRESH_INTERVAL_MINUTES`, the new cadence ceiling on `CatalogSource`, which
    `refresh_interval_minutes` is now validated against. A first attempt at one hour was still
    short of the 6-hour cadence most sources use and left the cold-start deferral in place — the
    measured retry counts stopped exploding but paused runs persisted, so the floor was resized
    against the ceiling rather than against a guessed cadence.
  - **Why this is not a weakening of ADR-005.** Retention never hands back spent capacity: an idle
    bucket refills from its own recorded timestamp and caps at burst, exactly as a busy one does.
    The previous behaviour was strictly *less* faithful — an expiring bucket forgot the tokens it
    had just spent, and the cold-start rule existed to compensate. Genuine state loss still
    cold-starts empty and throttle-first; `test_redis_loss_cold_starts_empty_without_a_source_burst`
    still passes unchanged.
  - **Evidence.** 1,367 unit · 279 integration · ruff and strict mypy clean over 266 files. Two new
    Redis integration tests pin the regression (a bucket idle past its refill window still grants;
    a retained bucket still enforces the shared rate) and a composition test pins retention against
    the cadence ceiling. In the running stack, Redis went from **0 retained pacer buckets to 15**,
    and pacer-caused pauses stopped.
  - **Unrelated environment noise seen during the session:** a host DNS blackout at 22:54–22:55
    failed every in-flight source with `[Errno -2] Name or service not known` and self-healed; this
    is the known hotspot-DNS behaviour, not an ingestion defect.
  - **What the fix then uncovered — OPEN, needs an owner ruling on crawl budget.** With the Pacer no
    longer deferring them, both BiblioCommons all-branch sources ran to completion and hit a real
    defect the thrashing had been masking: they exceed their **owner-reviewed page caps**. The
    adapter pages 25 items at a time until a short page or `page_limit`, so
    `san-jose-public-library-events` ceilings at 160 x 25 = 4,000 items against a best-ever haul of
    **3,820** (4.5% headroom), and `sccld-all-physical-branches-events` at 50 x 25 = 1,250 against
    **1,207** (3.4%). Both feeds have simply grown past caps sized when they were smaller. These are
    the only 2 of 29 real sources still behind schedule. `page_limit` is part of the reviewed source
    record and raising it increases approved third-party egress, so it is an owner decision, not a
    code fix. Recommended: roughly double both (160 -> 320, 50 -> 120) to restore headroom.

- **2026-08-25 — Calendar selection and the six-month grid restyled.** Owner: "looks ugly when
  selecting."
  - **Selection was an inverted light fill.** `.calendar-grid button.is-selected` set
    `background: var(--ink); color: var(--black)`, so the selected day read as a hole punched
    through a dark grid, and every topic chip inside it needed a second set of colours to stay
    legible (three override rules existed only to repaint chips for a white background). Selection
    now keeps the cell on the dark surface: an accent-tinted background, an inset accent ring, and
    the day total rendered in the accent. Chips keep the one palette they were designed against and
    the light-background overrides are gone. The week grid carried the same inverted fill and was
    brought in line.
  - **The six-month grid could not hold its own content.** A compact cell measures **29px wide with
    19px of usable width**. It was rendering the date plus up to three *numbered* topic pills on one
    row; the pills carried `min-width: 14px` and so could not shrink, spilled across day borders,
    and stretched whole rows out of alignment — visible as digits running together between adjacent
    days. The cell now spends its height instead of fighting for width: date, then day total, then a
    tone strip pinned to the bottom. The date never shrinks (losing which day a cell is costs more
    than losing its count), every count remains in the cell's `aria-label` and in each tone's
    `title`, and `overflow: hidden` plus a fixed `grid-auto-rows` guarantee a cell can never bleed
    into its neighbour however many topics a day carries.
  - **Two regressions caught by looking at the render, not the tests.** A first pass let the date
    shrink to zero width behind the count badge, so populated cells lost their day number entirely;
    the date is now `flex: 0 0 auto`. A second pass made the week view's selected count an
    accent-filled pill, which stretched into a solid lime bar across the column because that count
    is not a pill in the week header — it is now accent text.
  - **Also fixed while verifying:** week columns rendered no preview events at all. The per-day
    preview introduced with the summary work requested only 4 events, and a day window matches
    events that began earlier and are still running, which sort first — a busy column's whole page
    could be leftovers. Previews now read a full page and take the first four that actually start
    that day.
  - **Evidence.** Measured in the running stack with Playwright: **every compact cell exactly 56px,
    0 cells overflowing, 0 dot rows clipped**, 28 week preview chips across 7 columns. 104 calendar/
    event web tests (5 new assertions pinning the accent-ring selection and the fixed compact box),
    216 web tests overall, 40 Chromium e2e, ruff + strict mypy clean, production frontend build
    green. Month, week, and six-month selection states each screenshot-checked against live data.

- **2026-08-25 — Opening an event card no longer re-requests the list it belongs to.** Owner:
  "when selecting an event it shows loading panels... it should show it instantly."
  - **Root cause.** Every browsing action routes through `pushConsumerSnapshot`, which rebuilds the
    whole snapshot and calls `setFilters(nextSnapshot.filters)`. Expanding a card changes only
    `expandedId`, but the snapshot still hands back a **structurally identical filter object with a
    new identity**. The catalog effect depends on `filters`, so it re-ran; `loadCatalog`
    unconditionally clears its list and raises its loading flag, and `EventsView` renders skeletons
    whenever `loading && !events.length`. Opening a card therefore threw away the results the reader
    had just clicked into and fetched the same page again. The pre-existing
    `preserveExpandedOnNextCatalogLoad` ref is evidence the reload was known about; it carried the
    expanded id *through* the reload rather than preventing it.
  - **Fix.** `catalogFilterKey(filters)` in `lib/catalog-filters.ts` is now the canonical identity of
    a catalog request — every field that changes which events are returned, plus `sort`, and nothing
    about browsing state. `applyFilters` adopts a filter set through that key and keeps the previous
    object when the request is unchanged, so React bails out of the render entirely. All four filter
    setters (initial history restore, popstate, snapshot push, filter-bar change) go through it, and
    the calendar range effect got the same guard.
  - **One definition, not two.** `catalogSummarySignature` now derives from the same
    `catalogFilterKey` (with `includeSort: false`, since ordering cannot change a count), so the
    calendar cache and the request path can never disagree about what counts as a change.
  - **Evidence.** Measured against the running app with Playwright: expanding a card in a 72-card
    list leaves **72 cards, renders the expansion, shows no skeleton, and issues 0 catalog
    requests**. 105 event/calendar web tests (a new one pins the guard and forbids any path setting
    filters straight from a snapshot), 217 web tests overall, 40 Chromium e2e, ruff + strict mypy
    clean, production build green.

- **2026-08-25 — The price composer accepts an amount.** Owner: "how can we better implement price
  filter so users can input number."
  - **The dead end.** `parseSmartFilterComposerQuery` already recognised a `price` composer context
    (aliases `price`/`cost`/`budget`), and place, source, topic, and date all narrow by the term the
    reader types after it. Price was the **only** context that discarded its term: the branch
    returned the four categorical options and ignored everything after the keyword. So typing
    `price` confirmed the composer understood you, and typing `price 25` then returned **nothing at
    all** — measured, not inferred. A ceiling was reachable only by already knowing an exact
    phrasing (`price $25`, `price under 25`) or by leaving the omnibox for the filter rail's popover.
  - **A bare amount is a ceiling, but only in the composer.** `maximumPriceSuggestion` now takes
    `allowBareAmount`, set when the composer context is `price`. `price 25`, `budget 40`, `cost 15`,
    `price 5`, and `price 12.50` all resolve to a maximum. Outside the composer a bare number is
    still not a price: `25` and `2026` are far more often a year, a street number, or part of a
    title, and the existing announced forms (`$25`, `under 25`, `25 dollars`, now also `25 bucks`)
    continue to work everywhere. The two-character minimum term length is relaxed to one in the
    price composer, because `5` is a complete answer.
  - **The number is now visible, not guessed.** Opening the composer lists the four categories plus
    one-tap ceilings (`Up to $10/25/50/100`), so a reader learns that an amount is an option without
    having to discover the phrasing.
  - **Evidence.** Verified against the running app: `price` offers `Any price · Free · Paid · Price
    unlisted · Up to $10 · Up to $25`; `price 25` offers `Up to $25`; `budget 40` offers `Up to
    $40`; applying it yields the chip `PRICE ≤ $25`, the URL `max=25`, and the request
    `price_max_cents=2500`. 19 filter-suggestion tests (5 new, including one asserting a bare number
    outside the composer is *not* read as a price), 222 web tests overall, 40 Chromium e2e, web
    typecheck and production build green.

- **2026-08-26 — A host's page showed 2 events while the host published 87; every entity in the
  catalog had the same defect.** Owner: "for entities, e.g. sf commons there's much more events
  associated with them... make sure it's all captured for this and other entities."
  - **The catalog was reading a shelf and calling it a feed.** All Luma coverage came from one
    enabled source, `luma-sf`, mode `luma_discover_json`. Discover is a curated, ranked
    *one-event-per-calendar* shelf: a live walk of the whole Bay Area cursor ends naturally after
    **4 pages with 81 events drawn from 75 distinct owner calendars** — 1.08 events per host. Its
    `page_limit` of 40 was inert, because the ceiling is Luma's, not ours. `luma-nyc` measured
    identically (90 events, 89 calendars). The consequence was catalog-wide, not local to one page:
    **1,995 of 2,506 entities held exactly one event**, and every entity we had came from one of
    the two Discover shelves or the two Meetup city pages.
  - **The one adapter that could read a host's programme was fenced shut in code.** Mode
    `luma_calendar_json` already existed and already walked a calendar cursor, but
    `_PROFILES` was a hard-coded dict with a single entry, and migration 0117 had disabled that
    entry in 2026-07 on the assumption that the Discover cursor superseded it. It had not.
    `_profile_for_source` now derives the calendar identity from the reviewed row's `seed_url`
    itself and refuses any seed that is not the exact cursor shape, so admitting a calendar is an
    audited `catalog_sources` change — revision-bumped and written to
    `catalog_source_configuration_audit` — instead of an edit to a Python allowlist no audit trail
    covers. 64 host calendars are now reviewed and enabled (0163, 0168), on the rule that Discover
    materially under-represents them: **3 or more future events at review, personal calendars
    excluded**.
  - **Four defects only the live fleet could surface.** A calendar may list an *off-platform* event
    (`platform: "external"`) carrying no Luma identity at all — 7 of Postman's 17 entries — and one
    of them failed the whole refresh. A live custom slug contains dots (`luma.com/fw.models.nyc`)
    and the slug pattern rejected it, taking its calendar to zero. `registration_availability` has
    a fifth public value, `coming-soon`, that the mapping did not know; five such events published
    nothing for a New York calendar, and the same record on a Discover page would have taken that
    whole city feed down. And `page_limit` is a **cliff, not a budget** — exceeding it discards
    every page already fetched — which is why every calendar row carries 3x headroom.
  - **Calendar events would have been second-class.** A listing carries identity, timing, place and
    price and nothing else; description, speakers, partners, attendance and registration status
    live only on `api2.luma.com/event/get`, and only the Discover adapter followed that lane. Since
    both sources can hold the same event and the catalog keeps whichever fetch ran last, *which
    source refreshed last* would have decided what the reader saw. The lane is now
    `adapters/luma_detail.py`, shared by both adapters (0169 approves the second origin), and
    `_lease_seconds_for` reserves for the events a page cap can carry rather than for pages alone —
    both Luma modes had been leaning on the 300-second process default.
  - **Adding sources split the entities they were meant to fill.** Source-scoped identity is keyed
    to the asserting source, by 0147's deliberate rule that a display name is not a cross-source
    identity. Correct — until two sources assert *the same role, for the same name, on the same
    canonical event*, where the shared event has done the identifying and keeping them apart splits
    one host's evidence. That went from rare to routine overnight: **75 names across 164 rows**,
    and `The SF Commons` became two entities, one holding 2 events and one holding 86. 0171 keys a
    corroborated identity to the minimum source of its whole **connected component** — a
    direct-neighbour minimum is not symmetric, so a hub shelf and its leaf calendars would each
    choose differently and never converge — and moves standing mentions off the non-representative
    members, because a component is discovered only when its *second* source arrives and by then
    the first source's rows are already persisted. Testing caught that: the merge was
    order-dependent until the re-key step existed. Profile-verified identity is untouched.
  - **Nothing measured any of this, which is why it was found by eye.** Every completeness guard in
    the ingestion path is a *truncation* detector — page caps, cursor checks, lease fences. None can
    see a complete walk of the wrong seed. `quality/catalog_coverage.py` and
    `fn_report_catalog_source_coverage_v1` (0166) measure shape instead: **depth**, live future
    events per distinct organizer, read only where it means something. A shelf scores ~1.0 by
    construction; a host calendar scores its programme. It also reports what nothing else does —
    future events the newest-successful-run retention rule has silently retracted, page caps that
    have gone terminal, and sources aged past their own cadence. `make catalog-coverage`.
  - **Fixed while verifying.** The entity-intelligence worker had been throwing `ProgrammingError`
    every 30s since 23:24Z: a deployed image still called `fn_get_catalog_entity_insights_v1` after
    0156 dropped it, and the handler logged only `error_type`, so the missing function's name never
    reached the log. It now logs the message and escalates on a run of identical failures, because
    a drift is not a blip. Two BiblioCommons feeds (0167) had grown past caps sized when they were
    smaller — San José 3,820 of 4,000 (attempt_count 5,166, failing 13 days), SCCLD 1,207 of 1,250
    (2,252 attempts, 6 days) — publishing nothing while heading every cadence plan. The cap is
    pinned in *two* places, code and registry, and they must agree; an integration test now says so.
    `alameda-county-library-all-physical-branches-events` is at 8.3% headroom and is next.
  - **Evidence.** Measured through the new report's own rule (distinct live future events on each
    source's newest successful run): Luma now holds **895**, of which **723 come from the 64 host
    calendars that did not exist before** — the two Discover shelves contribute 172 between them
    (78 Bay Area, 94 New York) and that is all they have ever been able to contribute.
    `The SF Commons`, on the same entity id and URL the owner opened, **2 → 88 events**. 695 of 811
    calendar events now carry a description where none did. Duplicate source-scoped rows
    **164 → 51**, the remainder genuinely uncorroborated (an "April" in New York and an "April" in
    San Francisco share no event and stay two). `make catalog-coverage` exits 0 with no
    error-severity finding. 1,494 unit tests pass, plus 36 source-registry and 5 entity-identity
    integration tests — the entity projection had no executable coverage at all before this. ruff
    and strict mypy clean across every file touched.
  - **Caught by reviewing the change set adversarially** (28 findings raised, 26 refuted under
    independent attack, 2 confirmed and fixed):
    - The first cut of the coverage report counted observation *rows*, not events:
      `catalog_event_observations` is keyed by source identity and one source routinely observes
      one canonical event under several, so San José reported 3,986 for 3,883. Small in absolute
      terms and fatal in kind — `depth` divides two numbers that were counting different things.
      0173 counts distinct events, uses `IS DISTINCT FROM` (a source that had *never* succeeded
      reported zero retracted events rather than all of them, because both sides of the comparison
      were NULL), and adopts the retention rule's own window.
    - **The calendar lane never filtered `visibility`.** Discover drops non-public entries before
      the detail lane sees them; the calendar contract does not assert visibility at all. So an
      `unlisted` event would have been published whenever its detail record happened to agree — and
      would have failed the *entire* calendar whenever it did not, because the shared detail lane
      requires `visibility == "public"`. Now filtered, and never even fetched.
    - **0171 could strand an entity on a source that stopped asserting it.** Such a source appears
      in neither branch of the component computation, so a repair driven by the component map never
      selected its entity, and the prune spared it because it still held the other members'
      mentions. Reproduced end-to-end: after the naming source drops the event, a per-source
      refresh and a full rebuild disagree — and a later third source re-creates the exact split
      0171 exists to eliminate. 0175 reads the implied identity from the mentions an entity
      *actually holds*: the component's representative where a holder is still a member, otherwise
      the smallest source still asserting. Live invariant now holds at zero violations.
    - **The entity-intelligence log leaked third-party PII.** Logging `str(error)` renders
      SQLAlchemy's `[SQL: …]`/`[parameters: …]` tail and Postgres's `DETAIL: Failing row contains`,
      which bind a named individual's public profile URL — observed live in stdout, undeduplicated.
      The four sibling poll loops log the type alone and say why. The diagnosis this worker needs is
      on line one and the bound values are on the lines below it, so it now logs the first line,
      bounded.
    - Two migration docstrings asserted things that were not true — 0169 cited a Luma pacer envelope
      that does not govern this path (catalog refresh runs under `PUBLIC_JSONLD`'s generic default;
      what paces these GETs is the adapter's own per-host floor), and 0163's "600 events" assumed a
      full page everywhere. Both corrected. `_lease_seconds_for` is now the pure, testable
      `catalog_refresh_lease_seconds`, because it is the *real* upper bound on a Luma `page_limit`:
      past 90 pages the reservation exceeds the one-hour ceiling and the source is skipped with no
      run recorded at all. The registry test asserts both ends.
  - **Left open, owner-scoped.** The two Discover shelves are *still* shelves (`luma-nyc` names 87
    hosts at 1.08 each, `meetup-sf` 35 at 1.34); the coverage eval now says so on every run. Host
    calendars were registered by hand from one measured crawl — the systematic answer is a source
    *proposal* plane, since every Discover page already carries `calendar.api_id` and
    `calendar.slug` and throws them away. Meetup group calendars are the same untapped depth behind
    a different adapter. 1,355 future events remain retracted fleet-wide by the newest-run retention
    rule.

## Session contract reminder

Buildout phases run **Fable 5 / Opus at ultracode effort**. Orchestrate substantive phases with the Workflow
tool (fan-out → adversarial verify → synthesize → completeness critic); stay in the loop between workflows.
