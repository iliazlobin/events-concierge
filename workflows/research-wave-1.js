export const meta = {
  name: 'events-concierge-research',
  description: 'Deep research for a multi-tenant, fully-autonomous Events Concierge: agent orchestration, autonomous browser registration, event-source APIs, autonomous-action safety, multi-tenancy/secrets, calendar, ranking, clean architecture, cost',
  phases: [
    { title: 'Research', detail: '9 parallel researchers, one per dimension' },
    { title: 'Verify', detail: 'adversarial web-check of load-bearing claims' },
    { title: 'Write', detail: 'one verified dossier file per dimension' },
    { title: 'Synthesize', detail: 'research brief + completeness critic' },
  ],
}

const OUT_DIR = '/Users/iliazlobin/Claude/events-concierge/research'

const CONTEXT = `CONTEXT — the system under design:
A production-grade, MULTI-TENANT Events Concierge product. A user makes a natural-language request ("find me something Friday evening after work and sign me up"); the system DISCOVERS candidate events, RANKS them to the user's taste and constraints, autonomously REGISTERS / RSVPs (including PAID tickets) on the event's own site, and adds the confirmed event to the user's CALENDAR with de-duplication. It tracks each event through a lifecycle: found -> registered -> scheduled_to_calendar.

Confirmed scope (HARD):
(1) Shareable MULTI-USER product — authentication, per-user profiles + preferences + STORED THIRD-PARTY credentials (users' Google, Luma, Meetup, Eventbrite logins), tenant isolation, product-grade SLAs. NOT a single-user personal tool.
(2) HYBRID discovery — curated API/connector retrieval where a source exposes an API or feed, PLUS browser-driven discovery and registration (Claude-in-Chrome / computer-use) for API-less, JS-heavy, or invite-only sites (Luma, Partiful).
(3) FULLY AUTONOMOUS including paid/irreversible actions — no per-action human confirmation gate. Safety lives in per-user authorization, spending limits, idempotency, and auditability (configured POLICY, not interactive prompts).

This is a REBOOT of a 2025 prototype (LangGraph StateGraph supervisor + AutoGen MultimodalWebSurfer over Chrome CDP + OpenSearch function_score ranked index + Google Calendar API + OpenAI gpt-4o-mini, Python 3.11) re-architected on the 2026 Claude agent stack (Claude Agent SDK / native tool-use loop + Claude-in-Chrome + Claude Opus/Sonnet + MCP tools). The 2026 stack is a THESIS to validate with evidence, NOT a settled decision.

Design values: clean architecture, SOLID, ports and adapters (per-source adapters where an API adapter and a browser adapter satisfy ONE port), idempotent money-moving actions, full auditability, security-first handling of stored user credentials. This is backend/agent system design; frontend and deployment come later; the final tech stack is deliberately open.`

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

const DIMS = [
  { key: 'agent-orchestration-2026', title: 'Agent orchestration for a stateful multi-step task-completion agent (2026)', focus: `How to orchestrate a long-running, stateful agent that carries a request through discover -> rank -> register -> calendar with retries and partial failure. Compare, from primary docs and engineering writeups: the 2025 LangGraph StateGraph supervisor pattern (what it gives: MessagesState, checkpointing/MemorySaver, human-in-the-loop interrupts, graph topologies) vs the Claude Agent SDK / native tool-use agent loop (agentic loop, sub-agents, MCP tool integration, sessions, hooks) vs durable-execution engines (Temporal, Restate, Inngest, AWS Step Functions) for the money-moving multi-step saga. Cover: where the lifecycle state machine (found/registered/scheduled) should live (in the agent context vs an external durable store); how to model long-running waits (a registration that needs email confirmation) and resumption; supervisor/multi-agent vs single-agent-with-tools trade-off for THIS task; what genuinely changed 2025 -> 2026 in agent frameworks. Be concrete about interfaces and failure/resume semantics, not framework marketing.` },
  { key: 'autonomous-browser-registration', title: 'Autonomous browser automation for form-filling and paid registration', focus: `THE most execution-critical dimension. The agent must autonomously complete registration/RSVP/checkout forms on live event sites. Compare, from primary sources and real benchmarks: Anthropic computer use / Claude-in-Chrome extension (capabilities, permissions model, reliability, cost, current status 2026), Playwright/CDP scripted automation, AutoGen MultimodalWebSurfer (the 2025 choice), and the agentic-browser wave — OpenAI Operator/ChatGPT agent, Google Project Mariner, Browser Use, Stagehand (Browserbase), Skyvern, MultiOn, Manus. Establish: measured reliability on real web-task benchmarks (WebArena, WebVoyager, Online-Mind2Web, etc.) and what accuracy means for AUTONOMOUS irreversible actions; how each handles logged-in sessions and persistent auth/cookies; CAPTCHA / anti-bot / bot-detection reality on ticketing and RSVP sites (document the reality WITHOUT evasion techniques); headless-at-scale economics and latency for a MULTI-TENANT product running many concurrent sessions; failure modes (hallucinated clicks, wrong field, double-submit) and how to detect/guard them. Concrete benchmark numbers and per-action cost/latency are gold.` },
  { key: 'event-source-landscape', title: 'Event-discovery source & API landscape — the data-access tier list', focus: `For the HYBRID discovery model, tier every major event source into API-friendly vs browser-only. For each, establish from CURRENT (2026) official docs: Eventbrite (public search API status — they restricted/removed public event search; what remains), Meetup (GraphQL API, Pro/paid gating, what is queryable), Luma / lu.ma (any public API vs browser-only; RSVP flow), Ticketmaster Discovery API, SeatGeek Platform API, Partiful, Dice.fm, Bandsintown, Songkick, Resident Advisor, Fatsoma, Universe, Facebook/Meta Events (API deprecation state), Google Events / knowledge-panel event results, and city/venue calendars. Capture per source: endpoint shape + auth, rate limits, whether SEARCH is available or only lookup, data richness (price, location, capacity), and crucially the ToS position on AUTOMATED account creation / RSVP / ticket purchase by a third-party agent. Also: schema.org/Event JSON-LD adoption on event pages as a discovery signal. Which sources REQUIRE the browser tier and which are clean API. Concrete endpoints with examples are gold.` },
  { key: 'autonomous-action-safety', title: 'Autonomy, authorization, payments & safety for agents taking money-moving actions', focus: `Load-bearing given full-autonomy scope: the agent registers, RSVPs, and BUYS tickets on users' behalf with no per-action prompt. Research the safe-agentic-action patterns: delegated authorization and consent models (what does a user authorize once, and how is scope bounded); per-user SPENDING LIMITS and policy engines; idempotency keys for exactly-once money-moving actions; the transactional outbox / saga pattern with COMPENSATION for a multi-step register-then-calendar flow (how do you cancel/refund if a later step fails); auditability and immutable action logs for disputes; the emerging AGENTIC COMMERCE / agent-payments landscape as of 2025-2026 (OpenAI x Stripe Agentic Commerce Protocol, Visa Intelligent Commerce / Mastercard Agent Pay, Google AP2 Agent Payments Protocol, virtual/single-use card tokens for agents, 3-D Secure challenges that block full autonomy); liability and chargeback exposure when an autonomous agent purchases the wrong thing; whether to store the user's card, use the event site's stored payment, or issue a scoped virtual card. Document guardrails as POLICY (limits, allowlists, dry-run, kill-switch), not interactive confirmation.` },
  { key: 'multitenancy-auth-secrets', title: 'Multi-tenancy, authentication & storage of users third-party credentials', focus: `Load-bearing given product scope. The product stores each user's credentials for THIRD-PARTY sites (Google, Meetup, Eventbrite logins, and possibly passwords/session cookies for sites with no OAuth). Research: multi-tenant SaaS isolation models (pooled vs silo vs bridge; row-level security vs schema-per-tenant vs db-per-tenant) and which fits an agent product handling sensitive per-user secrets; the product's own auth (OIDC/OAuth2, session vs token); secrets management for third-party credentials at rest — envelope encryption with a KMS (AWS KMS, GCP KMS, Vault Transit), per-tenant data keys, what NEVER to store; OAuth token storage and refresh for Google Calendar and any OAuth event sources; the security/compliance implications of holding users' passwords or session cookies for sites lacking OAuth (this is the riskiest data in the system — what are the accepted patterns, e.g. credential vaulting a la Plaid/password managers, and their threat models); GDPR/CCPA duties for this credential data (deletion, breach). Concrete: key hierarchy, rotation, blast-radius containment.` },
  { key: 'calendar-integration', title: 'Multi-user calendar integration, dedup & conflict detection', focus: `The final step writes confirmed events to the user's calendar without duplicates. Research from primary docs: Google Calendar API for a MULTI-USER app (OAuth2 scopes, per-user tokens, events.insert/list, incremental sync with syncToken, push notifications via watch/channels, quotas and per-user rate limits, batch); Microsoft Graph calendar and Apple/CalDAV for broader coverage; DE-DUPLICATION strategy — matching an about-to-be-added event against existing calendar entries (by source event id stored in extendedProperties, by fuzzy title+time+location match), idempotency so a retried insert does not double-book; free/busy and CONFLICT detection before committing a registration (do not register the user for two overlapping events); timezone correctness; how to represent the source event id and registration state on the calendar entry for later reconciliation. Concrete API fields, scopes, and quota numbers.` },
  { key: 'ranking-personalization', title: 'Event ranking, relevance & per-user personalization', focus: `The 2025 build used OpenSearch function_score over popularity, uniqueness, venue quality, food availability, proximity. Research the 2026 options and their cost/quality trade-offs: hybrid retrieval (BM25/keyword + dense vector embeddings) over an event corpus; LLM-as-ranker / listwise reranking (Cohere Rerank, cross-encoders, or an Opus/Sonnet reranking pass) and its cost and latency at product scale; per-user PREFERENCE modeling (explicit profile + implicit signals from past attended/declined events), and how to fold personalization into the score; cold-start for a new user; whether a self-hosted OpenSearch/Elasticsearch index is still warranted vs a managed vector store (pgvector, Pinecone, etc.) vs live API retrieval with in-agent LLM ranking; how to keep ranking explainable to the user; deduping the same real-world event surfaced by multiple sources before ranking. Ground the cost claims for any LLM-ranking approach in current token pricing.` },
  { key: 'clean-architecture-agentic', title: 'Clean architecture, ports & adapters, and reliability patterns for an agentic pipeline', focus: `The owner demands clean architecture without slop. Research how hexagonal / ports-and-adapters / dependency inversion apply to an AGENTIC, effectful, money-moving pipeline: the domain core (User, EventRequest, CandidateEvent, Registration, the found/registered/scheduled lifecycle state machine) vs adapters (source connectors, the browser tool, calendar, payment, the LLM itself as a driven port); how ONE SourcePort is satisfied by both an API adapter and a browser adapter (and how the orchestrator picks); anti-corruption layers over messy third-party event schemas normalizing to schema.org/Event; reliability patterns for the multi-step saga — idempotent upsert, transactional outbox, at-least-once + idempotent handlers, retry/DLQ, compensation/rollback, the outbox vs event-sourcing choice for the action log; keeping the domain pure while the LLM/browser side is inherently nondeterministic; TESTING and EVALUATION of agentic flows — recorded HTTP fixtures/contract tests per adapter, golden transcripts, offline evals for the agent's decisions, replay; observability/tracing for agent runs (OpenTelemetry GenAI, LangSmith-style traces). Concrete interface boundaries, not platitudes.` },
  { key: 'cost-efficiency-scale', title: 'Cost, efficiency & scale economics for a multi-tenant autonomous agent', focus: `Make the product economically viable. Build a back-of-envelope cost model for one end-to-end request and for N users x M requests/week. Cover, with CURRENT (2026) pricing: Claude model token costs (Opus vs Sonnet vs Haiku) for the reasoning loop, and model tiering — cheap model for routing/extraction, expensive model only for hard registration reasoning; prompt caching and its savings for the repeated system/context; the compute + latency + cost of running headless/computer-use BROWSER sessions per registration (this may dominate); batching and concurrency limits per tenant; caching discovery results across users to avoid re-fetching the same events; rate-limit and quota budgeting across many tenants against shared third-party APIs; where the dominant cost sits (LLM tokens vs browser compute vs infra) and the sensitivity to each. Also unit-economics framing: cost per successful registration, and what pricing/limits keep it sustainable. Concrete numbers tied to cited pricing pages.` },
]

function researchPrompt(dim) {
  return `${CONTEXT}

YOUR RESEARCH DIMENSION: ${dim.title}

FOCUS:
${dim.focus}

METHOD:
- You have web access: if WebSearch/WebFetch are not loaded, load them first via ToolSearch ("select:WebSearch,WebFetch").
- Run 8-15 targeted searches; FETCH and read primary sources (official API docs, RFCs, papers, benchmark leaderboards, engineering blogs, well-maintained OSS repos/code). Avoid SEO listicles and content farms.
- It is July 2026 — check currency (APIs, model pricing, agent frameworks, and legal/commerce protocols change fast; verify against current official docs where possible).
- Record concrete specifics: exact endpoint shapes, header/scope names, benchmark numbers, token prices, thresholds, verbatim quotes — not vague generalities.
- Mark load_bearing=true on findings a design decision would hinge on (max ~5 of them).
- confidence: high = verified against a primary source you actually fetched; medium = single credible source; low = inferred or secondhand.

Return findings via the structured output schema. Each finding's detail must be dense and specific (2-6 sentences). design_implications = directives for OUR system's design.`
}

function verifyPrompt(dim, f) {
  return `You are an adversarial fact-checker with web access (load WebSearch/WebFetch via ToolSearch "select:WebSearch,WebFetch" if needed).

CLAIM (from research on "${dim.title}", feeding a multi-tenant autonomous Events Concierge design):
"${f.claim}"

SUPPORTING DETAIL: ${f.detail}
CITED SOURCES: ${(f.source_urls || []).join(' ') || '(none cited)'}

Try to REFUTE or CORRECT this claim using primary sources — fetch the cited sources AND search independently. It is July 2026; stale-API, stale-pricing, and stale-legal/commerce-protocol claims are the most common failure. Verdicts:
- confirmed: a primary source verifies it as stated
- adjusted: directionally right but needs correction (provide corrected_claim)
- refuted: wrong or outdated
- unverifiable: cannot be confirmed from accessible sources (treat marketing claims and secondhand numbers skeptically)
Provide evidence_urls for whatever you conclude.`
}

function writePrompt(dim, path, bundle) {
  return `Write a professional research dossier to ${path} using the Write tool (run Bash "mkdir -p ${OUT_DIR}" first only if the directory is missing).

AUDIENCE: the architect of a multi-tenant, fully-autonomous Events Concierge (clean architecture, production-grade). Dense, specific, zero fluff, no emoji, no marketing tone.

STRUCTURE:
# <Dossier title>
> One line: why this dimension matters for the Events Concierge.
## Verified findings
(Fold in the fact-check verdicts: DROP refuted claims — or keep with an explicit "REFUTED:" prefix only if the refutation itself is instructive; apply corrected_claim text from adjusted verdicts; tag load-bearing claims [confirmed] / [adjusted] / [unverifiable]. Non-load-bearing findings keep their researcher confidence tag.)
## Design implications
(Directives for OUR system.)
## Sources
(Annotated links.)

RESEARCH (JSON):
${JSON.stringify(bundle.research)}

FACT-CHECK VERDICTS (JSON):
${JSON.stringify(bundle.verdicts)}

Your final message must be exactly the file path you wrote.`
}

function synthPrompt(files) {
  return `Read every research dossier in ${OUT_DIR} (files: ${files.join(', ')}) and write a synthesis to ${OUT_DIR}/00-research-brief.md using the Write tool.

${CONTEXT}

The brief is the bridge from research to design. Structure:
# Research Brief — Events Concierge (multi-tenant, autonomous)
## What we are building (1 short para — multi-tenant product, hybrid discovery, fully autonomous incl. paid actions)
## Architecture direction the evidence supports (agent-orchestration + lifecycle state model; tiered source adapters API-vs-browser; browser-automation choice for autonomous registration; ranking approach)
## Autonomy, authorization & payments posture (spending limits, idempotency, agentic-commerce reality, guardrails-as-policy)
## Multi-tenancy, auth & credential-security posture
## Calendar integration & dedup direction
## Key facts, numbers & cost model for back-of-envelope (cite dossier files)
## What we deliberately change from the 2025 prototype (explicit contrast with LangGraph + AutoGen + OpenSearch + gpt-4o-mini)
## Open questions for the design phase

Max ~2500 words. Professional, dense, zero slop, no emoji. Cite dossier files by name so every claim is traceable.

Your final message (returned to the orchestrator, not shown to a human): a max-400-word executive summary of the brief.`
}

function criticPrompt(files) {
  return `You are a completeness critic. Read ${OUT_DIR}/00-research-brief.md, then skim the dossiers (${files.join(', ')}).

MISSION CONTEXT: ${CONTEXT}

Identify what is MISSING or WEAK in this research foundation before system design starts. Consider: design-critical questions left unanswered; load-bearing claims still tagged unverifiable; missing modality (e.g., no primary-source check of a key event-source API, no currency check on an agentic-commerce protocol or model price, no measured browser-automation reliability number); autonomy/liability assumptions unexamined; multi-tenant credential-security threat model gaps; scale/cost assumptions unmeasured; operational failure modes (double-purchase, mid-saga failure) unresearched. Do NOT list nice-to-haves — only gaps that would change or endanger the design. Return via schema.`
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
    const num = String(i + 1).padStart(2, '0')
    const path = `${OUT_DIR}/${num}-${dim.key}.md`
    return agent(writePrompt(dim, path, bundle), { label: `write:${dim.key}`, phase: 'Write', effort: 'low' }).then(() => path)
  }
)

const files = results.filter(Boolean)
log(`${files.length}/${DIMS.length} dossiers written to ${OUT_DIR}`)

const summary = await agent(synthPrompt(files), { label: 'synthesize:research-brief', phase: 'Synthesize', effort: 'xhigh' })
const gaps = await agent(criticPrompt(files), { label: 'critic:completeness', phase: 'Synthesize', schema: GAPS, effort: 'high' })

return { files, brief: OUT_DIR + '/00-research-brief.md', summary, gaps }
