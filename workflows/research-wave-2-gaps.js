export const meta = {
  name: 'events-concierge-gap-remediation',
  description: 'Gap-remediation wave 2 for the Events Concierge (free-RSVP launch, API-first): per-source ToS/CFAA, live source-tier re-verify + act-on-behalf auth, Google CASA/OAuth gate, durable-execution engine + saga idempotency, credential-injection + worker isolation, capacity vs per-source quotas, inbound-email ingestion for magic-link/OTP login + confirmation',
  phases: [
    { title: 'Research', detail: '6 parallel researchers, one per surviving design-endangering gap' },
    { title: 'Verify', detail: 'adversarial web-check of load-bearing claims' },
    { title: 'Write', detail: 'one verified dossier file per gap' },
    { title: 'Synthesize', detail: 'gap-remediation addendum + remaining-blockers critic' },
  ],
}

const OUT_DIR = '/Users/iliazlobin/Claude/events-concierge/research'

const CONTEXT = `CONTEXT — the system under design:
A production-grade, MULTI-TENANT Events Concierge product. A user makes a natural-language request; the system DISCOVERS candidate events, RANKS them, autonomously REGISTERS / RSVPs on the event's own site, and writes the confirmed event to the user's CALENDAR with de-duplication, tracking a found -> registered -> scheduled_to_calendar lifecycle.

REFINED SCOPE (HARD, decided after research wave 1):
- MULTI-USER product: auth, per-user profiles + STORED THIRD-PARTY credentials (Google, Meetup, Eventbrite, Luma logins), tenant isolation, product SLAs.
- LAUNCH = FREE RSVP ONLY. Autonomous free RSVP/registration (Luma, Meetup, Partiful, Eventbrite free events). PAID ticket purchase is DEFERRED behind policy flags — the payment port (ACP delegate_payment / AP2 agent tokens) is kept only as a forward-looking abstraction, NOT a launch dependency. Do NOT re-research payment rails, SCA/3-D Secure, or chargeback liability — those are out of scope for this wave.
- SOURCE POSTURE = API-FIRST, BROWSER BEST-EFFORT. API-clean sources (Ticketmaster/SeatGeek discovery, Meetup, Bandsintown/Songkick) are first-class and SLA-guaranteed; browser-only sources (Partiful, Eventbrite discovery, Luma registration) are best-effort with human-handoff on failure, off the reliability-SLA critical path.
- FULLY AUTONOMOUS within free RSVP: no per-action confirmation; safety via policy limits, idempotency, auditability.

Architecture direction already established by wave 1 (do NOT relitigate; build ON it): ports & adapters with one SourcePort (API adapter preferred, browser adapter fallback) normalizing to schema.org/Event; the found/registered/scheduled lifecycle in an EXTERNAL durable-execution engine (Temporal-class), one workflow per (user,event), workflowId as idempotency key; Claude native tool-use loop for reasoning inside activities; browser automation via the programmatic Computer Use API + DOM adapters (NOT the Claude-in-Chrome extension, which mandates an un-disable-able approval gate); ranking via Postgres+pgvector hybrid RRF + Cohere cross-encoder; bridge tenant isolation with a siloed KMS-envelope credential vault; 3-tier Claude model routing (Haiku/Sonnet/Opus).

This is a REBOOT of a 2025 prototype (LangGraph + AutoGen MultimodalWebSurfer + OpenSearch + gpt-4o-mini). Design values: clean architecture, ports & adapters, idempotent actions, auditability, security-first credential handling. This wave closes the specific design-endangering gaps a completeness critic raised after wave 1.`

const FINDINGS = {
  type: 'object',
  required: ['dimension', 'findings', 'design_implications', 'sources'],
  properties: {
    dimension: { type: 'string' },
    findings: { type: 'array', items: { type: 'object', required: ['claim', 'detail', 'confidence', 'load_bearing'], properties: {
      claim: { type: 'string' },
      detail: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
      load_bearing: { type: 'boolean' },
      source_urls: { type: 'array', items: { type: 'string' } },
    }}},
    design_implications: { type: 'array', items: { type: 'string' } },
    sources: { type: 'array', items: { type: 'object', required: ['title', 'url'], properties: {
      title: { type: 'string' }, url: { type: 'string' }, note: { type: 'string' },
    }}},
  },
}

const VERDICT = {
  type: 'object',
  required: ['verdict', 'explanation'],
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'adjusted', 'refuted', 'unverifiable'] },
    explanation: { type: 'string' },
    corrected_claim: { type: 'string' },
    evidence_urls: { type: 'array', items: { type: 'string' } },
  },
}

const GAPS = {
  type: 'object',
  required: ['gaps'],
  properties: { gaps: { type: 'array', items: { type: 'object', required: ['gap', 'why_it_matters', 'proposed_followup'], properties: {
    gap: { type: 'string' }, why_it_matters: { type: 'string' }, proposed_followup: { type: 'string' },
  }}}},
}

// Wave-2 dossiers are numbered 10-15 (wave 1 used 01-09).
const DIMS = [
  { key: 'per-source-legal-tos', title: 'Per-source Terms-of-Service, automated-access & CFAA posture for free RSVP on stored user credentials', focus: `The product logs into each user's OWN account on third-party sites and RSVPs on their behalf via automation. Even with NO payment, most consumer-site ToS prohibit automated access and/or credential sharing. For EACH target — Luma (lu.ma), Meetup, Eventbrite, Partiful, and (discovery/read-only) Ticketmaster + SeatGeek — find and quote the CURRENT (2026) ToS / API terms / developer agreement clauses on: automated access / scraping / bots; acting on behalf of a user / agents; account sharing and credential use; and any explicit allowance or prohibition of programmatic RSVP. Cover the general US legal posture for automating a user's OWN account with their consent: CFAA scope after Van Buren (2021) and hiQ v LinkedIn final outcome (authorized-access vs public-data distinctions), and whether first-party-consented automation changes the analysis; account-ban / IP-block operational risk to the SERVICE and to the USER; GDPR/CCPA angle only insofar as it touches storing the credential. OUTPUT a per-source matrix: rsvp_on_behalf posture (allowed / grey / prohibited), the governing clause quoted, and the recommended default for a per-source automation_allowed policy flag. This directly gates which sources ship enabled at launch. Do NOT research payment/purchase legality (deferred).` },
  { key: 'source-tier-reverify-2026', title: 'Live 2026 re-verification of the source tier list, act-on-behalf RSVP auth & rate limits', focus: `The per-source API-vs-browser fork is THE central launch decision, and API terms change yearly. Against CURRENT (2026) official developer docs (fetch them), re-verify for each source the FREE-RSVP path specifically: MEETUP — GraphQL API: is there an RSVP/attendance mutation, what AUTH does it need (member OAuth acting as the user vs a Pro/organizer key), is RSVP-on-behalf actually permitted, and the exact rate limit (the ~200 points/req-hr claim) and its point-cost model; LUMA — confirm the public API is management/owner-only with NO self-RSVP endpoint (so RSVP is browser-only), and what OAuth/identity exists; EVENTBRITE — the free-ticket ORDER/registration flow: is there an API to register/place a free order as the user, or is it browser-only, plus confirm public event SEARCH is still dead (2019 removal); PARTIFUL — API reality (almost certainly browser-only), RSVP flow; TICKETMASTER Discovery v2 + SEATGEEK Platform — for DISCOVERY/read only (5000/day, 5rps, 1000-item deep-paging cap; SeatGeek partner-gating status); BANDSINTOWN + SONGKICK — read-only discovery API status in 2026; SerpApi google_events as a cross-source discovery top-of-funnel (pricing, coverage). OUTPUT the definitive per-source adapter decision (API vs browser for discover(); API vs browser for register()), the auth model for act-on-behalf, and the quota — superseding the wave-1 dossier 03 where 2026 docs differ.` },
  { key: 'google-oauth-casa-gate', title: 'Google/Microsoft/Apple calendar OAuth verification & CASA Tier 2 launch gate', focus: `Calendar write is core and its OAuth verification is a schedule/budget gate. From Google's CURRENT (2026) OAuth API verification and CASA documentation (fetch primary docs): the exact mapping of scopes to NONsensitive / SENSITIVE / RESTRICTED tiers and which trigger CASA (Cloud Application Security Assessment) Tier 2 (annual independent DAST); specifically, does a CALENDAR-ONLY app using calendar.events (or the narrower calendar.app.created) + calendar.freebusy — and NO Gmail scopes — avoid restricted-scope verification and CASA Tier 2, or does calendar.events itself count as sensitive/restricted; the realistic verification TIMELINE (weeks/months) and the annual third-party DAST COST range; incremental authorization (request calendar scope only at the add-to-calendar step) as a scope-minimization tactic; the refresh-token issuance/limit model (per-user-per-client cap and the ~50/100-token silent-invalidation churn) and one-refresh-token-per-user guidance. Also briefly: Microsoft Graph calendar (delta query, subscription caps, app verification/publisher-verification) and Apple/iCloud CalDAV (app-specific passwords, no formal verification) as the multi-provider CalendarPort adapters. OUTPUT the launch OAuth plan: minimal scope set, whether calendar-only dodges CASA Tier 2, and the timeline/budget to plan for.` },
  { key: 'durable-execution-engine', title: 'Durable-execution engine selection + the saga / idempotency spine (incl. browser-path double-RSVP)', focus: `Wave 1 concluded the found/registered/scheduled lifecycle belongs in an external durable-execution engine because Agent-SDK/LangGraph checkpoint-resume replays side effects. VERIFY that failure mode against a primary source or a concrete reproduction (how transcript/checkpoint resume re-executes an already-performed side effect), then COMPARE the leading 2026 durable-execution options against THIS system's needs: Temporal, Restate, DBOS (Postgres-backed, since we already run Postgres), and Inngest. Criteria to establish from primary docs: idempotency / exactly-once activity primitives; the PAYLOAD SIZE LIMIT (e.g. Temporal ~2MB) and the recommended pattern of offloading bulky screenshots / DOM / candidate-event lists to object storage referenced by key; DURABLE TIMERS + SIGNALS to model "registration needs email confirmation" as a wait rather than a blocking agent turn; self-host vs managed cloud operational cost; language/SDK fit (Python/TypeScript). Also specify the BROWSER-PATH idempotency/confirmation strategy (downgraded from double-charge to double-RSVP now that paid is deferred, but still real): the merchant POST carries no idempotency key, so before any activity retry the agent must RE-QUERY the user's registration state on the site (order/RSVP history), use confirmation-detection heuristics, and route ambiguous outcomes to human review rather than blind re-submit. OUTPUT an engine recommendation with rationale + the saga structure (steps, idempotency keys, compensations, timers).` },
  { key: 'credential-injection-worker-isolation', title: 'Credential-injection build-vs-buy + browser-worker compromise threat model & lifecycle', focus: `The browser worker must hold a user's plaintext session cookie/password in memory to drive a site login — the least-analyzed, highest-value exposure in the system. TWO decisions. (1) BUILD-VS-BUY injection: does 1Password Secure Agentic Autofill (and/or Browserbase's integration) actually support UNATTENDED, policy-scoped auto-approval for a SERVER-SIDE MULTI-TENANT service — i.e. no per-request human tap per login — as of 2026? Fetch 1Password's current Agentic Autofill / developer docs and support material and establish whether headless policy auto-approval exists; if it does NOT, spec the self-built alternative: a KMS-envelope injection worker that decrypts the per-tenant credential in-memory and types it into the login form without the secret ever entering an LLM prompt/tool-arg/transcript/log. (2) WORKER-COMPROMISE threat model: since a single shared browser worker could exfiltrate many tenants' credentials, research and specify per-tenant / per-session worker ISOLATION (ephemeral containers or microVMs, no cross-tenant secret coresidency), egress/network controls, secret zeroization after use, and prompt-injection defense at the browsing boundary. Plus the CREDENTIAL LIFECYCLE: detecting/handling an upstream password change (re-auth prompt to the user), revocation, preferring short-TTL session cookies over stored passwords, and anomalous-use / stolen-cookie-replay detection. OUTPUT: the injection build-vs-buy recommendation + a worker-isolation & credential-lifecycle spec.` },
  { key: 'capacity-quota-model', title: 'Throughput / capacity model against per-source quotas and Anthropic org limits', focus: `Turn the scaling ceilings into a concrete capacity model. Confirm from CURRENT (2026) docs the per-source rate limits (Meetup ~200 req/point-hr, Eventbrite ~2000/hr, Luma 200/min per calendar, Ticketmaster 5000/day + 5 rps + 1000-item deep-paging cap) and the Anthropic ORG-LEVEL limits (usage tiers: RPM / input-TPM / output-TPM, e.g. Tier 4 ~4000 RPM / 2M ITPM, and how prompt caching read-tokens are excluded from the per-minute input limit); include the managed-browser concurrency ceiling (Browserbase ~25-100 concurrent sessions per plan). Then BUILD THE MODEL: for a target of, say, N in {1k, 10k, 100k} active users each making ~M requests/week WITH a bursty peak (everyone's "Friday evening" request lands in the same window), compute (a) per-source API call volume for discovery + RSVP and where it hits each quota — is Meetup's ~200/hr a hard central wall, and does drawing against EACH USER's own stored credential (so quota is per-user, not per-service) plus a global fair-share scheduler actually clear it; (b) the Anthropic RPM/ITPM draw across discovery + ranking + registration reasoning under the 3-tier model routing, and whether prompt caching + Batch keep it under tier limits; (c) concurrent browser-session demand at the burst peak vs the Browserbase ceiling and the self-host threshold; (d) the real bottleneck (source quota vs Anthropic limit vs browser concurrency) that binds FIRST and at what user count, and the per-confirmed-RSVP cost at each scale. State assumptions explicitly. OUTPUT the capacity plan: first-binding ceiling, mitigations (per-user credential fan-out, fair-share scheduler, caching/Batch, org/workspace sharding, self-host threshold), and the scale at which each ceiling bites.` },
  { key: 'email-ingestion-architecture', title: 'Autonomous inbound-email ingestion for magic-link / OTP login and registration confirmation', focus: `Load-bearing and unexamined by wave 1: the agent must autonomously (a) complete email MAGIC-LINK / email-OTP logins on the flagship free-RSVP browser targets, and (b) receive and parse registration/RSVP CONFIRMATION emails to resolve the durable-timer+signal that advances found -> registered -> scheduled. First establish from current docs/flows which targets actually use email-OTP/magic-link vs password: Luma (passwordless email code / magic-link), Partiful, Meetup, Eventbrite — and how often a fresh login/confirmation email is required. Then research the architecture options and their security + COMPLIANCE tradeoffs, because this collides with the "avoid Gmail scopes to dodge CASA Tier 2" plan (probe google-oauth-casa-gate):
- Option 1 — read the USER's primary inbox: Gmail API (users.watch + history.list push, or messages.list polling) and the scope cost (gmail.readonly / gmail.modify are RESTRICTED scopes forcing CASA Tier 2 + restricted-scope verification + annual DAST); Microsoft Graph mail; IMAP. Quantify the compliance escalation and credential blast-radius.
- Option 2 (likely preferred) — provision a PER-USER dedicated concierge email ALIAS/RELAY that the user signs up with on event platforms, so every magic-link/OTP/confirmation lands in an inbox WE control, sidestepping the user's primary inbox AND the Gmail restricted scope AND CASA escalation. Research inbound-email infrastructure (AWS SES inbound receipt rules -> S3/SNS/Lambda, Postmark inbound, Mailgun routes, Cloudflare Email Routing), per-user addressing (unique subdomain-per-user vs plus-addressing vs random alias), deliverability/DMARC, and whether event platforms accept aliased/plus addresses at signup.
- Option 3 — user-side forwarding rule that forwards ONLY event-platform mail to our relay (no restricted scope).
- Parsing: reliably extracting a magic-link URL or OTP code from HTML email; one-time-use link handling; the RACE between the browser worker waiting on the login/confirm page and the email arriving (coordinate via the durable engine's signal/timer); and treating email as UNTRUSTED content (prompt-injection vector) where the magic link is itself a secret (never log, short TTL).
OUTPUT: a recommended inbound-email architecture (per-source OTP/magic-link incidence; the alias/relay-vs-Gmail decision with its CASA impact; and the login+confirmation flow design showing how the mail-ingestion subsystem and the browser worker coordinate through the durable engine).` },
  { key: 'meetup-rsvp-prerequisite-chain', title: 'Meetup RSVP prerequisite chain — does createEventRsvp work for non-members, and can group-join be automated', focus: `LOAD-BEARING: under the refined scope Meetup is the ONLY autonomous register() path on the reliability SLA (Luma = browser best-effort/off-SLA; Eventbrite = discovery-only/human-handoff; Partiful = disabled). Wave 2 verified only that the GraphQL mutation createEventRsvp(eventId, response) EXISTS, is callable via per-user OAuth, and is rate-limited ~500 points/60s. It did NOT verify the PREREQUISITE CHAIN, and the whole on-SLA register promise rests on it. From Meetup's CURRENT (2026) GraphQL developer docs, schema (introspection references), API changelog, and developer-forum / community evidence, establish:
- (1) Does createEventRsvp SUCCEED when the token-owner is NOT already a member of the hosting group? Document the actual behavior: silent auto-join, hard error, or pending/approval state. Quote the docs or forum threads.
- (2) Is group MEMBERSHIP automatable via the API — is there a joinGroup-class mutation exposed, and does joining BYPASS or BLOCK on the common gates real Meetup groups impose: organizer approval queue, intro/screening questions, and paid membership dues? Which of these are representable/answerable via API at all?
- (3) What fraction of typical target Meetup events sit in OPEN (instant-RSVP) groups vs APPROVAL-GATED / questionnaire / dues-required groups? Any published stats, Meetup help-center statements, or credible estimates.
- (4) The Meetup Pro / API consumer LICENSE terms behind the single OAuth consumer: revocation at "sole discretion", audit rights, non-sublicensable / multi-tenant-on-behalf clauses, and any policy on acting for many end-users under one Pro consumer. What is the graceful-degradation posture if Meetup throttles or revokes the consumer (the sole on-SLA path)?
Where the runtime behavior CANNOT be confirmed from docs/forums, say so explicitly and specify the exact BUILD-TIME API SPIKE to run against a live Pro token + test accounts (mirroring the deferred browser-success-rate spike). OUTPUT: the realistic Meetup launch coverage claim (all events vs open-RSVP-groups-only vs already-a-member-only), the group-join automation posture, and the license-revocation fallback plan.` },
]

function researchPrompt(dim) {
  return `${CONTEXT}

YOUR RESEARCH DIMENSION (a surviving design-endangering gap): ${dim.title}

FOCUS:
${dim.focus}

METHOD:
- You have web access: if WebSearch/WebFetch are not loaded, load them first via ToolSearch ("select:WebSearch,WebFetch").
- Run 8-15 targeted searches; FETCH and read primary sources (official API/developer docs, ToS/legal texts, engine docs, RFCs, court opinions, engineering blogs). Avoid SEO listicles.
- It is July 2026 — check currency; API terms, rate limits, OAuth/CASA policy, and legal rulings change. Verify against current official docs where possible.
- Record concrete specifics: quoted ToS clauses, exact endpoint/scope names, rate-limit numbers, verification timelines, verbatim quotes — not vague generalities.
- Mark load_bearing=true on findings a design decision would hinge on (max ~5).
- confidence: high = verified against a primary source you actually fetched; medium = single credible source; low = inferred or secondhand.
- STAY IN SCOPE: launch is FREE RSVP only; do NOT research payment rails, SCA/3-D Secure, or purchase legality (deferred).

Return findings via the structured output schema. Each finding's detail must be dense and specific (2-6 sentences). design_implications = directives for OUR system's design.`
}

function verifyPrompt(dim, f) {
  return `You are an adversarial fact-checker with web access (load WebSearch/WebFetch via ToolSearch "select:WebSearch,WebFetch" if needed).

CLAIM (from gap-remediation research on "${dim.title}", feeding a multi-tenant free-RSVP Events Concierge design):
"${f.claim}"

SUPPORTING DETAIL: ${f.detail}
CITED SOURCES: ${(f.source_urls || []).join(' ') || '(none cited)'}

Try to REFUTE or CORRECT this claim using primary sources — fetch the cited sources AND search independently. It is July 2026; stale ToS clauses, stale API/rate-limit facts, and stale OAuth/CASA policy are the most common failures. Verdicts:
- confirmed: a primary source verifies it as stated
- adjusted: directionally right but needs correction (provide corrected_claim)
- refuted: wrong or outdated
- unverifiable: cannot be confirmed from accessible sources (treat marketing claims and secondhand numbers skeptically)
Provide evidence_urls for whatever you conclude.`
}

function writePrompt(dim, path, bundle) {
  return `Write a professional research dossier to ${path} using the Write tool (run Bash "mkdir -p ${OUT_DIR}" first only if the directory is missing).

AUDIENCE: the architect of a multi-tenant, free-RSVP-launch, API-first Events Concierge (clean architecture, production-grade). Dense, specific, zero fluff, no emoji, no marketing tone.

STRUCTURE:
# <Dossier title>
> One line: which post-wave-1 gap this closes and why it matters.
## Verified findings
(Fold in the fact-check verdicts: DROP refuted claims — or keep with an explicit "REFUTED:" prefix only if instructive; apply corrected_claim text from adjusted verdicts; tag load-bearing claims [confirmed] / [adjusted] / [unverifiable]. Non-load-bearing findings keep their researcher confidence tag.)
## Design implications
(Directives for OUR system. Where this is a per-source matrix or a decision, state it as a concrete table/list.)
## Sources
(Annotated links.)

RESEARCH (JSON):
${JSON.stringify(bundle.research)}

FACT-CHECK VERDICTS (JSON):
${JSON.stringify(bundle.verdicts)}

Your final message must be exactly the file path you wrote.`
}

function synthPrompt(files) {
  return `Read the six gap-remediation dossiers in ${OUT_DIR} (${files.join(', ')}) AND the original wave-1 brief ${OUT_DIR}/00-research-brief.md, then write an addendum to ${OUT_DIR}/00b-gap-remediation-addendum.md using the Write tool.

${CONTEXT}

The addendum records how wave 2 closed the post-wave-1 gaps and what it changes. Structure:
# Research Brief Addendum — Gap Remediation (wave 2)
## Scope refinement applied (free-RSVP launch, API-first/browser-best-effort) and what it removed from the risk surface
## Per-source decision matrix (source -> discover() API|browser, register() API|browser, act-on-behalf auth, rate limit, ToS posture, ship-enabled-at-launch?) — the definitive launch source list
## Durable-execution engine decision + saga/idempotency spine (incl. browser-path re-query rule)
## Google/calendar OAuth & CASA Tier 2 launch plan (scope set, does calendar-only dodge Tier 2, timeline/budget)
## Credential-injection build-vs-buy + browser-worker isolation & credential lifecycle
## Capacity plan vs per-source quotas and Anthropic limits (the real bottleneck)
## Corrections/updates to the wave-1 brief (call out where 2026 re-verification changed a wave-1 claim, e.g. dossier 03)
## Gaps now CLOSED vs DEFERRED (explicitly: per-site browser success-rate = deferred empirical build-time spike; payment rails/SCA = deferred with paid scope)

Max ~2000 words. Professional, dense, zero slop, no emoji. Cite dossier files (01-15) by name so every claim is traceable.

Your final message (returned to the orchestrator, not shown to a human): a max-350-word executive summary of what wave 2 settled and what remains open for the design phase.`
}

function criticPrompt(files) {
  return `You are a completeness critic. Read ${OUT_DIR}/00b-gap-remediation-addendum.md and skim the wave-2 dossiers (${files.join(', ')}) plus ${OUT_DIR}/00-research-brief.md.

MISSION CONTEXT: ${CONTEXT}

After TWO research waves, identify ONLY the gaps that would still change or endanger the DESIGN given the refined free-RSVP / API-first scope. Ignore anything already correctly deferred (per-site browser success measurement as a build-time empirical spike; payment rails / SCA with paid scope). Consider: load-bearing claims still unverifiable; a per-source decision still unresolved; a security or idempotency hole in the free-RSVP saga; a capacity ceiling unmodeled. Do NOT list nice-to-haves. If the research foundation is now sufficient to start design, say so with few or zero gaps. Return via schema.`
}

phase('Research')
const results = await pipeline(DIMS,
  (dim) => agent(researchPrompt(dim), { label: `research:${dim.key}`, phase: 'Research', schema: FINDINGS, effort: 'high' }),
  async (res, dim) => {
    if (!res) return null
    const lb = (res.findings || []).filter(f => f && f.load_bearing).slice(0, 4)
    const verdicts = await parallel(lb.map(f => () =>
      agent(verifyPrompt(dim, f), { label: `verify:${dim.key}`, phase: 'Verify', schema: VERDICT, effort: 'medium' })
        .then(v => v ? Object.assign({ claim: f.claim }, v) : null)
    ))
    return { research: res, verdicts: verdicts.filter(Boolean) }
  },
  (bundle, dim, i) => {
    if (!bundle) return null
    const num = String(i + 10)
    const path = `${OUT_DIR}/${num}-${dim.key}.md`
    return agent(writePrompt(dim, path, bundle), { label: `write:${dim.key}`, phase: 'Write', effort: 'low' }).then(() => path)
  }
)

const files = results.filter(Boolean)
log(`${files.length}/${DIMS.length} gap dossiers written to ${OUT_DIR}`)

const summary = await agent(synthPrompt(files), { label: 'synthesize:gap-addendum', phase: 'Synthesize', effort: 'xhigh' })
const gaps = await agent(criticPrompt(files), { label: 'critic:remaining-blockers', phase: 'Synthesize', schema: GAPS, effort: 'high' })

return { files, addendum: OUT_DIR + '/00b-gap-remediation-addendum.md', summary, gaps }
