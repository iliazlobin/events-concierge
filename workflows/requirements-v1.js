export const meta = {
  name: 'events-concierge-requirements',
  description: 'Draft the Events Concierge requirements spec grounded in the research corpus, then adversarially review it (3 lenses), fold every finding into v0.2, and closure-verify to sign-off-ready',
  phases: [
    { title: 'Draft', detail: 'one senior requirements engineer authors requirements.md v0.1 from the research corpus' },
    { title: 'Review', detail: '3 adversarial lenses (fidelity / completeness / testability) in parallel' },
    { title: 'Revise', detail: 'fold every finding into a clean v0.2 rewrite' },
    { title: 'Closure', detail: 'verify each finding RESOLVED/PARTIAL/MISSED + hunt new defects; verdict' },
  ],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const REQ = WS + '/design/requirements.md'
const EXEMPLAR = '/Users/iliazlobin/Claude/jobs-tracking-system/design/requirements.md'
const CRAFT = '/Users/iliazlobin/.claude/skills/project-design/references/requirements-craft.md'
const TEMPLATES = '/Users/iliazlobin/.claude/skills/project-design/references/templates.md'

const SCOPE = `PROJECT: Events Concierge — a production-grade, MULTI-TENANT product. A user issues a natural-language request ("find me something Friday evening after work and sign me up"); the system DISCOVERS candidate events across many sources, RANKS them to the user's taste + hard constraints, autonomously REGISTERS/RSVPs where the source permits, writes the confirmed event to the user's CALENDAR (dedup + conflict-gate), and RECONCILES it if the organizer cancels/reschedules or the user un-RSVPs.

SEVEN HARD SCOPE DECISIONS (from PROJECT.md — treat as fixed, do not relitigate):
1. Multi-user product: auth, per-user profiles + preferences + STORED THIRD-PARTY credentials, tenant isolation, product SLAs.
2. Hybrid discovery: API/connector retrieval where a source has an API, plus browser-driven (Computer Use API + DOM) for API-less/JS-heavy sites.
3. Fully autonomous within the permitted surface: no per-action confirmation; safety via configured POLICY (limits, idempotency, auditability, kill-switch), not interactive prompts.
4. LAUNCH = FREE RSVP ONLY. Paid purchase DEFERRED — the payment port stays a forward-looking abstraction behind a paid_allowed flag, NOT a launch build (no BOTS-Act/SCA/chargeback machinery at launch).
5. SOURCE POSTURE = API-FIRST, browser BEST-EFFORT (browser off the reliability-SLA critical path, human-handoff on failure).
6. POST-BOOKING LIFECYCLE IN v1: detect organizer cancel/reschedule (schema.org eventStatus via webhook/poll) + reconcile the calendar entry; support user-initiated un-RSVP (free = no refund complexity).
7. v1 FRAMING = "SHIP THE HONEST SPLIT": ALWAYS-autonomous discovery + ranking + calendar (+ reconciliation) across ALL sources; AUTONOMOUS RSVP where the source permits (Meetup EXISTING-GROUP events on-SLA + Luma browser best-effort off-SLA); PRE-FILLED ONE-TAP HUMAN-HANDOFF everywhere else (Eventbrite, Partiful, Meetup approval/dues/non-member groups) as a first-class product feature. Implication: TWO SLA CLASSES (autonomous lane vs handoff lane), a first-class human-handoff subsystem, and per-source-per-modality routing.

KEY ARCHITECTURE THE RESEARCH ESTABLISHED (evidence in research/00-research-brief.md + 00b-gap-remediation-addendum.md + dossiers 01-17 — cite dossiers where a number/threshold/mechanism is evidence-backed):
- Orchestration: external durable-execution engine (Temporal Cloud; DBOS fallback), one workflow per (user_id, event_id), workflowId as idempotency key, claim-check for the 2 MB payload cap, saga discover->rank->register/rsvp->await_confirmation->dedupe_calendar->write_calendar with compensation; email/OTP confirmation as durable timer+signal; Claude native tool-use loop for reasoning inside activities. (Claude Agent SDK sessions are NOT durable — resume replays side effects.)
- Clean architecture: LLM as a driven reasoning port; ONE SourcePort per source satisfied by an API adapter (preferred) + a browser adapter (fallback), deterministic capability-flag selection (never model choice); schema.org/Event canonical via an Anti-Corruption Layer; transactional outbox + immutable audit log; OTel GenAI tracing tagged by tenant.
- Browser automation: programmatic Computer Use API + DOM adapters, NOT the Claude-in-Chrome extension (un-disable-able approval gate). Realistic ~56-64% per-attempt success ceiling; detect-then-submit re-query rule to prevent double-RSVP (browser POST carries no server idempotency key); human-handoff on CAPTCHA/identity walls.
- Per-source launch matrix (definitive, dossiers 10/11): Meetup discover=API + register=API-only createEventRsvp (per-user OAuth; browser PROHIBITED by ToS) — ON SLA but ONLY for groups the user is already a member of (no joinGroup mutation; approval/screening/dues groups => human-handoff); Ticketmaster discover=API read-only; SerpApi google_events = cross-source discovery funnel; Luma discover=browser/SerpApi + register=browser (best-effort, off-SLA); Eventbrite discover=browser/SerpApi + register=NONE shippable (no order API + ToS bans browser) => discovery-only, register human-handoff; Partiful = DISABLED by default (ToS bans robots). automation_allowed is per-source, per-modality {api, browser}. Binding legal risk = contract + trespass + account/IP ban, NOT CFAA (Van Buren) => enforcement is per-user pacing, human-cadence, quarantine-on-ban circuit breaker; never IP-rotation/block-circumvention.
- Ranking: Postgres + pgvector hybrid retrieval (BM25 tsvector + dense ANN) fused by RRF (k~60) to ~100-200 candidates, then a cross-encoder rerank (Cohere Rerank v3.5); LLM listwise only as a top-10 tie-break; per-user LightGBM re-scoring layer; cold-start from onboarding prefs + content metadata + LLM intent inference; de-dupe the same real-world event across sources BEFORE ranking (retain all per-source registration URLs); mandatory freeBusy conflict gate before any RSVP.
- Multi-tenancy & credentials: bridge isolation — pooled app plane behind Postgres RLS (FORCE RLS, non-owner role, SET LOCAL tenant context, tenant_id leading index), SILOED credential vault; KMS envelope encryption, per-tenant DEK, tenant_id+credential_type as KMS encryption context (GDPR breach safe-harbor). OIDC + Backend-for-Frontend (httpOnly session cookie, tokens server-side, tenant_id a signed claim). Credential injection = BUILD a non-LLM KMS-envelope placeholder broker (LLM emits {{username}}/{{password}} tokens, broker decrypts in-memory, CDP domain-pinning to bound origin, Playwright fill, zeroize after type; secret structurally excluded from prompts/tool-args/Temporal history/logs) — 1Password Agentic Autofill is human-in-the-loop, rejected for the unattended path. Isolation = one ephemeral Firecracker/gVisor microVM per (user,event) login session, egress allowlist (target origin + KMS only), CaMeL dual-LLM boundary vs indirect prompt injection. OAuth wherever the source offers it (Meetup) so no password is stored; injection worker only for browser-only sources (Luma).
- Login-email ingestion: Luma/Partiful are PASSWORDLESS (email code / magic-link); autonomous login + confirmation need inbound mail. Solution = a PER-USER concierge relay inbox the user signs up with (e.g. alice@u.<domain>, SES->S3->Lambda / Cloudflare Email Routing), deterministic link/OTP extraction, Temporal signal — sidestepping the user's Gmail AND the restricted-scope CASA gate. Untrusted-email injection handling; magic-link is a secret (short TTL, never logged).
- Calendar: calendar-only Google scopes (calendar.events + calendar.freebusy) are SENSITIVE not RESTRICTED => dodge CASA Tier 2 entirely, but still need ~3-6 week sensitive-scope app verification (pre-launch critical path); MUST publish to Production (cannot ship on Testing: 7-day refresh-token expiry); one refresh token per user (100-token cap is per-client-ID); NEVER add a Gmail scope (flips whole client to restricted + annual DAST). Deterministic client-set event id (hash of tenant+source+sourceEventId) => insert, 409 duplicate => patch (idempotent upsert); extendedProperties.private dedup key + fuzzy secondary; mandatory freeBusy conflict gate; webhook incremental sync (syncToken; 410 => full resync; weekly channel renewal). CalendarPort with Google + Microsoft Graph + Apple CalDAV adapters; always IANA timeZone.
- Capacity: Ticketmaster shared 5,000/day app-key binds FIRST (~4-5k users) => a central read-through catalog cache (scheduled geo/category-sharded crawl -> Postgres; all tenant discovery reads hit cache) is LAUNCH ARCHITECTURE not optimization; per-user-OAuth sources (Meetup/Luma) fan out against each user's own token. Anthropic Scale tier = 10k RPM / 10M ITPM / 2M OTPM per model class; ITPM/OTPM (not RPM) binds; cache-read tokens excluded from ITPM; Batch API separate pool for ranking + post-RSVP summarization. Real operational bottleneck = Computer-Use browser concurrency (~40 concurrent at 100k users) — a first-class independently-scaled pool kept OFF the SLA with human-handoff on saturation. Unit economics ~$0.15-0.25 per confirmed RSVP (synchronous+caching), ~$0.08-0.12 via Batch; gated by a per-tenant RSVPs/period policy limit. Model tiering: Haiku parse/extract/dedup, Sonnet default register reasoning, Opus only on hard/failed cases.

THREE EMPIRICAL GATES (cannot be closed by research — they are build-time verification items; put them in the open-items ledger with resolution paths, and flag G2 as DESIGN-BLOCKING):
- G1 (estimation, requirements input): quantify the realized autonomous-on-SLA vs browser-best-effort vs human-handoff request mix by running a realistic NL-request corpus against the resolved source matrix -> sets the SLA + handoff-lane sizing.
- G2 (design-BLOCKING build-time spike): Meetup createEventRsvp — (a) does it auto-join an OPEN group for a non-member, (b) is the 500pt/60s quota per-token vs per-app vs per-IP, (c) is it retry-idempotent. Contingency carried NOW: if per-app quota => global fair-share queue + degrade-to-handoff (no fixed SLA number); add a read-RSVP-state-before-mutate guard on the API adapter.
- G3 (pre-design verification): do Luma/Eventbrite/Meetup signup validators ACCEPT per-user relay-inbox addresses? If rejected, the OTP/magic-link login path has no in-scope fallback (Gmail forbidden) — resolve before design freeze.

DEFERRED / OUT OF SCOPE v1: autonomous PAID ticket purchase (payment port = stubbed abstraction only); SCA/3DS/chargeback machinery; Partiful register; Eventbrite autonomous register; Meetup non-member/approval/dues-group auto-join; frontend/mobile UI; multi-region HA; SeatGeek/Songkick partner-gated licenses (feature-flag, not launch floor); advanced personalization LTR beyond the baseline LightGBM re-scoring (design-phase tuning).`

const FIND = { type:'object', required:['findings'], properties:{ findings:{ type:'array', items:{ type:'object',
  required:['severity','location','issue','proposed_fix'], properties:{
    severity:{type:'string', enum:['blocker','major','minor']},
    location:{type:'string'}, issue:{type:'string'}, proposed_fix:{type:'string'} }}}}}

const CLOSURE = { type:'object', required:['verdict','findings_status','new_defects','summary'], properties:{
  verdict:{type:'string', enum:['sign-off-ready','needs-another-round']},
  findings_status:{ type:'array', items:{ type:'object', required:['finding','status'], properties:{
    finding:{type:'string'}, status:{type:'string', enum:['RESOLVED','PARTIAL','MISSED']}, note:{type:'string'} }}},
  new_defects:{ type:'array', items:{ type:'object', required:['severity','issue'], properties:{
    severity:{type:'string', enum:['blocker','major','minor']}, issue:{type:'string'}, location:{type:'string'} }}},
  summary:{type:'string'} }}

const draftPrompt = `You are a staff-level requirements engineer. Author the REQUIREMENTS SPECIFICATION for the Events Concierge and WRITE it to ${REQ} using the Write tool (run Bash "mkdir -p ${WS}/design" first if needed).

${SCOPE}

BEFORE WRITING, read for grounding (use Read):
- ${WS}/PROJECT.md — the authoritative scope decisions (1-7) and design values.
- ${WS}/research/00-research-brief.md and ${WS}/research/00b-gap-remediation-addendum.md — the evidence base; where they conflict, the addendum (wave 2) wins.
- Skim the specific dossiers ${WS}/research/10-*.md ... 17-*.md for any number/threshold you cite.
- ${EXEMPLAR} — the FORMAT + QUALITY BAR to MATCH EXACTLY (structure, density, executable acceptance criteria, [owner target] convention, evidence citations). Study it before writing.
- ${CRAFT} and ${TEMPLATES} — requirements craft (EARS phrasing, INVEST, acceptance-criteria styles, ISO/IEC 25010 NFR checklist) and the requirements.md skeleton.

STRUCTURE (mirror the exemplar's 8 sections):
1. Purpose (what we are building, in the honest-split framing).
2. Confirmed scope (owner decisions, hard) — the 7 decisions above.
3. Definitions (table: Tenant/User, EventRequest, CandidateEvent, CanonicalEvent, Source, SourcePort/modality, Registration, RSVP lane {autonomous-on-SLA | browser-best-effort | human-handoff}, Credential, RelayInbox, the found->registered->scheduled->reconciled lifecycle, Saga, etc.).
4. Functional requirements — grouped FR-1..FR-n, each with numbered sub-requirements AND an *Acceptance:* block of executable AC-k checks that map 1:1 to the FRs. Ensure COVERAGE of: identity/tenancy/auth (OIDC+BFF, RLS bridge isolation); user profiles/preferences + third-party credential vault (KMS envelope, OAuth-first, injection broker, microVM isolation, credential lifecycle); event discovery (hybrid API+browser+SerpApi funnel, per-source-per-modality capability registry, TM central catalog cache, ACL to schema.org/Event, cross-source dedup before ranking); ranking & personalization (pgvector hybrid RRF + cross-encoder + LightGBM, freeBusy conflict gate, cold-start); registration/RSVP execution (SourcePort API+browser adapters, honest-split routing, detect-then-submit browser idempotency, read-before-mutate API guard, relay-inbox OTP/magic-link login); autonomy/authorization/safety (policy engine + per-user limits + kill-switch + immutable audit log; CaMeL injection isolation reader/actor split; payment-port seam stubbed); durable orchestration & lifecycle (Temporal saga, idempotency keys, compensation, durable timers/signals, post-booking reconcile + un-RSVP + change detection); calendar integration (calendar-only Google scopes, idempotent upsert, dedup, incremental sync, multi-provider port); human-handoff subsystem (FIRST-CLASS: pre-filled links, one-tap finish, handoff queue, notifications, fallback for browser-fail/CAPTCHA/approval-gated/deferred-register); compliance & per-source policy (automation_allowed/paid_allowed flags, ToS posture, ban circuit-breaker, GDPR credential deletion/erasure, no evasion). Reorganize/rename groups if cleaner, but cover all of it.
5. Non-functional requirements — NFR-1..n across ISO 25010, each with a MEASURED target and a measurement basis; tag provisional product goals [owner target] and evidence-backed numbers with the dossier. Cover at least: freshness/discovery latency; the TWO SLA classes (autonomous-lane RSVP success/latency vs handoff-lane time-to-handoff) — note the autonomous-lane SLA number is pending G1/G2; availability; scale envelope (users, events, browser concurrency ~40, TM 5k/day cache); cost envelope (~$0.15-0.25/confirmed RSVP); security (credential vault, injection isolation, zeroization); tenant isolation (RLS, no cross-tenant leakage); durability (no lost lifecycle events, idempotent, outbox); extensibility (ports; new source adapter = one module + registration); auditability (every action traceable, immutable log); privacy/GDPR (credential + PII erasure); the Google sensitive-scope verification pre-launch gate; DR (RPO/RTO).
6. Out of scope (v1) — paid purchase (port stubbed), Partiful register, Eventbrite autonomous register, Meetup non-member/approval-group auto-join, frontend/UI, multi-region HA, SeatGeek/Songkick partner licenses, advanced LTR.
7. Settled since research (key evidence outcomes that pin requirements) — brief bullets citing dossiers.
8. Open items feeding the design phase — a table (# | item | resolution path). MUST include G1, G2 (mark DESIGN-BLOCKING), G3, the per-site browser success-rate spike, the personalization-model lifecycle, and any genuine design trade-offs (durable-engine confirm, credential-lifecycle details, catalog-cache freshness, handoff-lane staffing/SLA).

Rules: EARS-style testable phrasing; every FR verifiable by at least one AC; NO invented precision (numbers are [owner target] unless dossier-cited); no tech smuggled beyond what the research settled; dense, professional, zero slop, no emoji. Header block = Version v0.1 / date placeholder / Basis (cite briefs + PROJECT.md decisions) / Notation ([owner target] convention). Since Date.now is unavailable, date the header "2026-07-02".

Your final message must be exactly the file path you wrote (${REQ}).`

function reviewPrompt(lens) {
  return `You are an adversarial reviewer of a requirements specification. Read ${REQ} in full, and read ${WS}/research/00-research-brief.md + ${WS}/research/00b-gap-remediation-addendum.md to check claims against the evidence.

${SCOPE}

YOUR LENS: ${lens.prompt}

Report ONLY defects that should change the document, most severe first, via the schema. Each finding: severity (blocker/major/minor), location (FR/NFR/AC id or section), issue (what is wrong), proposed_fix (concrete). Do not restate what is already correct.`
}

const LENSES = [
  { key:'fidelity', prompt:'FIDELITY to the evidence. Verify every number/threshold/named mechanism against the research corpus (briefs + dossiers). Flag: invented precision (a target presented as evidence), numbers that contradict the addendum (which supersedes wave 1 — e.g. Meetup 500pt/60s not 200/hr; calendar-only scopes sensitive not restricted; Meetup membership-gated), misattributed citations, and any FR that assumes a capability the research refuted (e.g. joinGroup, Eventbrite order API, Claude-in-Chrome extension for register, agent-token payment at launch).' },
  { key:'completeness', prompt:'COMPLETENESS at requirements altitude. What is MISSING? Systematically check cross-cutting concerns for THIS system: the human-handoff subsystem as first-class (not an afterthought); the two SLA classes; per-source-per-modality automation_allowed routing; credential lifecycle (rotation on upstream password change, revocation, needs_reauth halt); relay-inbox login for passwordless Luma; the post-booking reconcile + un-RSVP branch + change detection; injection isolation (reader/actor split, microVM, egress); tenant isolation + RLS; the Google sensitive-scope pre-launch verification gate; the TM central catalog cache as launch architecture; kill-switch + policy engine; audit log; GDPR erasure of credentials AND event history; DR/RPO-RTO; every research open-question and the 3 empirical gates. Only requirements-altitude items.' },
  { key:'testability', prompt:'TESTABILITY + consistency. Every FR must be verifiable — flag FRs with no covering acceptance criterion, weasel words ("robust", "efficient", "as needed"), NFRs with no measurement method, scope creep past the owner deferrals (paid purchase, Partiful register, multi-region), tech smuggled in where the research left a design-phase choice, internal contradictions, and duplicate/overlapping requirements. Confirm each AC is a concrete, runnable check (fixture-shaped), not a restatement of the FR.' },
]

function revisePrompt(findings) {
  return `You are the staff requirements engineer revising the Events Concierge requirements spec. Read the CURRENT draft ${REQ} in full, then produce a clean v0.2 that folds in EVERY finding below, and WRITE it back to ${REQ} (overwrite) with the Write tool.

${SCOPE}

REVIEW FINDINGS TO FOLD (every one must be addressed — resolve it, or if you judge it wrong, keep the doc but the closure agent will check your reasoning; prefer to fix):
${JSON.stringify(findings, null, 1)}

Rules: keep the exemplar structure + density; bump the header to Version v0.2 / 2026-07-02; add a short "Changes since v0.1" note listing how each finding class was resolved; preserve all correct content; every FR still maps to >=1 AC; no new invented precision. Dense, professional, zero slop, no emoji.

Your final message must be exactly the file path you wrote (${REQ}).`
}

function closurePrompt(findings) {
  return `You are a closure-verification auditor. Read the revised requirements spec ${REQ} in full, and the original review findings below. For EACH finding, judge whether v0.2 RESOLVED / PARTIAL / MISSED it. Then hunt for NEW defects the rewrite introduced (contradictions, dropped content, new invented precision, an FR that lost its AC). Finally give a verdict: "sign-off-ready" only if there are zero blockers and no MISSED blocker/major findings; else "needs-another-round".

${SCOPE}

ORIGINAL FINDINGS:
${JSON.stringify(findings, null, 1)}

Return via the schema. Be strict — a spec goes to the owner for sign-off on your verdict.`
}

phase('Draft')
const draftPath = await agent(draftPrompt, { label:'draft:requirements-v0.1', phase:'Draft', effort:'xhigh' })
log('v0.1 drafted: ' + draftPath)

phase('Review')
const reviews = (await parallel(LENSES.map(l => () =>
  agent(reviewPrompt(l), { label:`review:${l.key}`, phase:'Review', schema:FIND, effort:'high' })
    .then(r => r ? { lens:l.key, findings:(r.findings||[]) } : null)
))).filter(Boolean)
const allFindings = reviews.flatMap(r => r.findings.map(f => Object.assign({ lens:r.lens }, f)))
log(`${allFindings.length} findings across ${reviews.length} lenses`)

phase('Revise')
const v2 = await agent(revisePrompt(allFindings), { label:'revise:requirements-v0.2', phase:'Revise', effort:'xhigh' })
log('v0.2 written: ' + v2)

phase('Closure')
const closure = await agent(closurePrompt(allFindings), { label:'closure-verify', phase:'Closure', schema:CLOSURE, effort:'high' })

return { requirements: REQ, findings_count: allFindings.length, findings: allFindings, closure }
