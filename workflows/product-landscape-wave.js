export const meta = {
  name: 'events-concierge-product-landscape',
  description: 'Competitive/product-landscape research wave for the Events Concierge: commercial products, agentic-booking products, open source, indie builds, academic work, market postmortems, adjacent concierge services, demand signals',
  phases: [
    { title: 'Research', detail: '8 parallel landscape researchers, one per dimension' },
    { title: 'Verify', detail: 'adversarial web-check of load-bearing claims' },
    { title: 'Write', detail: 'one verified dossier file per dimension (18-25)' },
    { title: 'Synthesize', detail: 'product landscape brief 00c + product-opinion completeness critic' },
  ],
}

const OUT_DIR = '/Users/iliazlobin/Claude/events-concierge/research'

const CONTEXT = `CONTEXT — the product under design:
An "Events Concierge": a multi-tenant consumer product where a user makes a natural-language request ("find me something Friday evening after work and sign me up") and the system DISCOVERS candidate events across many sources, RANKS them to the user's taste and constraints, autonomously RSVPs/REGISTERS where the source permits, and writes the confirmed event to the user's CALENDAR, then keeps it reconciled (organizer cancel/reschedule detection, un-RSVP).

Confirmed launch scope (already settled, do not relitigate): multi-user product; hybrid discovery (API-first connectors + real-browser automation as best-effort fallback); FREE RSVP only at launch (paid ticket checkout deferred); "honest split" framing — always-autonomous discovery+ranking+calendar across ALL sources, autonomous RSVP where the source permits (Meetup groups the user already belongs to; Luma via browser best-effort), and a first-class pre-filled ONE-TAP HUMAN HANDOFF everywhere else (Eventbrite, Partiful, Meetup approval-gated groups) — "the concierge does 95%, you tap once".

A deep TECHNICAL research corpus already exists (agent orchestration, browser automation reliability, per-source API/ToS tier list, credential security, quotas, legal). THIS WAVE IS DIFFERENT AND MUST NOT REHASH IT. This wave maps the PRODUCT AND COMPETITIVE LANDSCAPE — every similar project out there: commercial products, general-purpose agentic assistants, open-source repos, indie/hobbyist builds and blog writeups, academic work, and dead startups — to feed a PRODUCT OPINION: who the user is, positioning, differentiation, the wedge, pricing, go-to-market, and any product requirements the current spec is missing.

Background: the owner is rebooting his own 2025 prototype (LangGraph supervisor + AutoGen MultimodalWebSurfer browser automation + OpenSearch function_score ranking + Google Calendar API; repo github.com/iliazlobin/events-planner-agents, demo youtube.com/watch?v=ORLfWH-2Zfc). Collect as much concrete information as possible: feature sets, pricing, traction (users/stars/funding), launch and shutdown dates, postmortem causes, architecture notes from OSS repos, and hard lessons from builders. It is July 2026 — verify currency; this space moved fast in 2025-2026.`

const FINDINGS = {
  type: 'object',
  required: ['dimension', 'inventory', 'findings', 'product_implications', 'sources'],
  properties: {
    dimension: { type: 'string' },
    inventory: { type: 'array', items: { type: 'object', required: ['name', 'status', 'what_it_does', 'relevance'], properties: {
      name: { type: 'string' },
      url: { type: 'string' },
      status: { type: 'string', description: 'active | dead(year) | pivoted | acquired | archived-repo | paper' },
      what_it_does: { type: 'string' },
      traction: { type: 'string', description: 'users/stars/funding/revenue signals, with numbers where found' },
      pricing: { type: 'string' },
      relevance: { type: 'string', description: 'what it teaches the Events Concierge — overlap, gap it exposes, or lesson' },
    }}},
    findings: { type: 'array', items: { type: 'object', required: ['claim', 'detail', 'confidence', 'load_bearing'], properties: {
      claim: { type: 'string' },
      detail: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
      load_bearing: { type: 'boolean' },
      source_urls: { type: 'array', items: { type: 'string' } },
    }}},
    product_implications: { type: 'array', items: { type: 'string' } },
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
  { key: 'commercial-event-discovery-products', title: 'Commercial event-discovery & social-planning products (2026 state)', focus: `Map the consumer products people actually use to find and attend events, and how far each has gone toward AI/concierge features. Cover at minimum: Luma (lu.ma - discovery feeds, calendars-as-social-graph), Meetup (incl. any AI features post-Bending-Spoons), Partiful (incl. its discovery ambitions), Posh (posh.vip), Fever (feverup - incl. its AI/personalization claims and Originals pivot), DICE, Bandsintown, Songkick, Resident Advisor, Eventbrite consumer app (incl. its AI/personalized feed efforts), TodayTix, Thursday/Timeleft-style social-connection apps, city-guide apps (Time Out, DoStuff/Do312 network), and any NEW 2025-2026 AI-native event-discovery startups (search for them - e.g. apps that build a taste profile and push picks). For each: feature set (discovery, personalization, social, RSVP, calendar), monetization, traction signals, and precisely which parts of our loop (discover -> rank -> register -> calendar -> reconcile) it covers vs leaves open. The key output: a feature-coverage picture showing where the AUTONOMOUS CLOSED LOOP is absent from every incumbent.` },
  { key: 'agentic-assistants-that-book', title: 'General-purpose agentic assistants that can book/RSVP on the web', focus: `The substitute threat and the pattern library: general agents that act on websites for users. Cover as of mid-2026: OpenAI Operator / ChatGPT agent mode (what booking/RSVP tasks it does, autonomy gates, pricing tier, real usage reports), Anthropic Claude computer use / Claude-in-Chrome consumer positioning, Perplexity Comet browser agent, Google Project Mariner / Gemini agent capabilities in Chrome, Amazon "Buy For Me"/Rufus agentic checkout, MultiOn/Please AI, Manus, Genspark, Lindy, browser-agent consumer products (Browser Use cloud, etc.). Establish: what event-ish tasks users actually delegate to them (evidence: reviews, Reddit, usage studies); their autonomy/confirmation UX (what they will and will not do unattended - especially logins, payments, RSVPs); reliability reputation; pricing; and the agentic-commerce merchant side (which merchants accept agent traffic vs block it - Ticketmaster/Eventbrite/Meetup stance on agents if stated). Key output: why a VERTICAL events concierge beats a horizontal agent (or where it does not), stated concretely.` },
  { key: 'open-source-event-aggregation', title: 'Open-source event aggregation, discovery & calendar projects', focus: `Sweep GitHub and the fediverse for open-source prior art. Cover: federated/self-hosted event platforms (Mobilizon, Gancio), open event management (Open Event / eventyay, Attendize, Hi.Events, pretix), event aggregators/scrapers (city-event scrapers, ical/RSS merge projects, "events in X" static-site generators, integrations in Home Assistant), event-recommendation demos, and LLM-agent event-planner demos comparable to the owner's own repo (search GitHub topics: event-discovery, event-aggregator, events-calendar, rsvp; also LangGraph/CrewAI/AutoGen example galleries with event-planner agents). For each notable repo: stars, last-commit recency, architecture in one line, what it solved and what it never solved. Also: any open datasets of local events, and community attempts at a common event schema/exchange beyond schema.org/Event. Key output: what exists to BUILD ON vs what everyone keeps failing to solve (fresh comprehensive local supply, dedup, RSVP automation).` },
  { key: 'indie-builds-and-writeups', title: 'Indie builds, blog writeups & community lessons (AI event finders/digests)', focus: `The builder literature: blog posts, Show HN / Hacker News threads, Reddit posts, IndieHackers projects, newsletters where individuals built AI event finders, weekly "things to do" digest bots, personal event concierges, calendar-filling agents, scrape-to-newsletter pipelines. Search hard: "I built an AI to find events", "event discovery side project", Show HN event aggregator, AI events newsletter generator, Telegram/Discord event-digest bots, twitter/X builds. For each: what they built, stack, what worked, why they stopped (data acquisition pain? retention? no willingness to pay?), any traffic/user numbers shared. Include curated-human alternatives that compete (local "things to do" newsletters/Instagram curators - e.g. The Skint-style) and what their traction says about the demand shape. Key output: the recurring failure modes and the recurring user delight moments across dozens of small attempts.` },
  { key: 'academic-event-recsys-web-agents', title: 'Academic prior art: event recommendation & web-agent booking tasks', focus: `Two literatures. (1) Event recommendation research: RecSys/CIKM/WSDM work on event-based social networks (the classic Meetup/Plancast/Douban datasets), geo-temporal recommendation, cold-start for one-off items (an event dies after it happens - no ratings accumulate; this is structurally different from movie recsys), group event recommendation, and any 2024-2026 LLM-for-event-recommendation papers. Extract: which signals matter empirically (geo distance, social, time-of-week, content), realistic accuracy ceilings, the established cold-start techniques for one-shot items. (2) Web-agent benchmarks as they touch OUR task: booking/RSVP/form-fill tasks inside WebArena, Mind2Web/Online-Mind2Web, WebVoyager, GAIA, AgentBench and 2026 successors - current SOTA success rates on transactional tasks and the failure taxonomies. Key output: what the literature says our ranking layer can and cannot achieve, and the empirical ceiling for autonomous web actions (only as it updates/confirms our existing ~56-64% number - do not re-derive the browser-automation stack).` },
  { key: 'market-failures-postmortems', title: 'Dead & pivoted event-discovery startups — postmortem patterns', focus: `Why does this category keep killing companies? Research the graveyard with dates, funding, and stated causes: IRL (unicorn, dead 2023 - 95% fake users), YPlan (sold for scraps to Time Out 2016), Sosh, Dojo (London, acquired), Fatsoma, Shindig-type startups, Songkick's lawsuit-then-sale arc, Facebook Events' decline as a discovery surface, Yahoo Upcoming, Zvents, Oh My Rockness, WillCall (acquired), Jukely (subscription concerts - what happened), Pogo/what-to-do apps, Fever's survival path (why did IT survive - Originals/owned inventory?), Meetup's repeated ownership changes and near-death. Also the aggregator-economics literature: why event data aggregation is a low-margin scrape war (supply fragmentation, organizers' incentive to post only on owned channels, ticketing platforms hoarding data as moat). Extract the repeatable failure taxonomy: (a) supply freshness/coverage cost, (b) low frequency of use -> retention death, (c) no monetization between discovery and ticket (affiliate crumbs), (d) chicken-and-egg local density. Key output: which failure modes our AUTONOMOUS CLOSED-LOOP + CALENDAR-NATIVE approach actually neutralizes vs still faces.` },
  { key: 'adjacent-concierge-scheduling', title: 'Adjacent "do-it-for-me" concierge & scheduling products — trust and UX patterns', focus: `Products whose lesson is the TRUST MODEL for acting on a user's behalf, not events per se. Cover: human/hybrid concierges (Yohana - Panasonic, status 2026; Duckbill; Magic; Fin-style assistants; the executive-assistant-as-a-service market), AI scheduling/EA products (Howie, Skej, Reclaim, Clockwise, Motion, Lindy meeting scheduling - how they earn calendar-write trust, their autonomy defaults, pricing $/mo), AI travel agents that BOOK (Mindtrip, Layla, Expedia/Booking.com agent features, Alaska/United NLU search - who actually completes bookings autonomously vs hands off), and restaurant-reservation snipers/assistants (table-snipe services, Resy/OpenTable bots controversy). Extract: how each product frames delegation and consent (upfront policy vs per-action confirm), where users draw the line on autonomy (research consistently shows a trust cliff at payment/identity), what they charge and what retention looks like, failure stories (wrong bookings, user backlash). Key output: the proven autonomy-UX patterns (digest+approve, policy budgets, one-tap confirm, undo windows) we should copy for the honest-split lanes, and pricing anchors for a concierge that acts (not just recommends).` },
  { key: 'demand-signals-user-jobs', title: 'Demand signals, user jobs & willingness to pay for event discovery/booking', focus: `Ground the product opinion in demand evidence. Research: how people report finding events today (surveys, Eventbrite/Meetup research reports, Pew social-capital work, "loneliness epidemic" + third-places literature as demand tailwind); the recurring complaint corpus (Reddit r/[city] "how do you find things to do", "Facebook Events is dead now what", fragmentation pain); search/SEO demand ("things to do in X this weekend" volumes, who wins that SERP); TikTok/Instagram as the de-facto event-discovery channel for Gen Z (evidence + numbers); demographic segments with acute need (new-in-town, remote workers, parents, dating/social-anxiety users, professional networkers); FOMO vs decision-fatigue framing; and any willingness-to-pay evidence for (a) curation/digests (paid newsletter conversion), (b) autonomous booking (concierge fees, Jukely-style subscriptions, ticket-sniper fees). Also: event ATTENDANCE no-show rates for free RSVPs (a known Meetup pathology - matters because our product auto-RSVPs; over-RSVP could worsen no-show and get users banned/organizers hostile). Key output: a defensible statement of WHO feels this pain hardest, WHAT job they hire an events concierge for, and WHAT they might pay.` },
]

function researchPrompt(dim) {
  return `${CONTEXT}

YOUR RESEARCH DIMENSION: ${dim.title}

FOCUS:
${dim.focus}

METHOD:
- You have web access: if WebSearch/WebFetch are not loaded, load them first via ToolSearch ("select:WebSearch,WebFetch").
- Run 10-18 targeted searches; FETCH and read primary sources (product sites/changelogs/pricing pages, GitHub repos, postmortems, funding databases coverage, HN/Reddit threads, papers). Avoid SEO listicles except as pointers.
- It is July 2026 — check currency (products die, pivot, and ship AI features fast; verify status against live pages where possible).
- Fill the INVENTORY: every relevant project/product/paper you find, even minor ones — breadth matters, the owner asked to "collect as much info on the topic as possible". Aim for 10-25 inventory entries with concrete traction/pricing numbers where they exist.
- FINDINGS are for cross-cutting claims (patterns, market facts, lessons). Mark load_bearing=true on findings the product opinion would hinge on (max ~5).
- confidence: high = verified against a primary source you actually fetched; medium = single credible source; low = inferred or secondhand.

Return via the structured output schema. product_implications = directives for OUR product opinion (positioning, wedge, pricing, missing requirements).`
}

function verifyPrompt(dim, f) {
  return `You are an adversarial fact-checker with web access (load WebSearch/WebFetch via ToolSearch "select:WebSearch,WebFetch" if needed).

CLAIM (from landscape research on "${dim.title}", feeding the product opinion for an autonomous Events Concierge):
"${f.claim}"

SUPPORTING DETAIL: ${f.detail}
CITED SOURCES: ${(f.source_urls || []).join(' ') || '(none cited)'}

Try to REFUTE or CORRECT this claim using primary sources — fetch the cited sources AND search independently. It is July 2026; stale product status (dead/pivoted/renamed), stale pricing, and inflated traction numbers are the most common failures in landscape research. Verdicts:
- confirmed: a primary source verifies it as stated
- adjusted: directionally right but needs correction (provide corrected_claim)
- refuted: wrong or outdated
- unverifiable: cannot be confirmed from accessible sources (treat marketing claims and secondhand numbers skeptically)
Provide evidence_urls for whatever you conclude.`
}

function writePrompt(dim, path, bundle) {
  return `Write a professional research dossier to ${path} using the Write tool.

AUDIENCE: the product owner + architect of an autonomous multi-tenant Events Concierge forming a product opinion. Dense, specific, zero fluff, no emoji, no marketing tone.

STRUCTURE:
# <Dossier title>
> One line: why this dimension matters for the Events Concierge product opinion.
## Landscape inventory
(One compact entry per project/product/paper from the inventory JSON: **Name** (status; url) — what it does; traction/pricing; relevance to us. Group into sensible sub-buckets. Keep EVERY inventory entry — breadth is the point.)
## Verified findings
(Fold in the fact-check verdicts: DROP refuted claims — or keep with an explicit "REFUTED:" prefix only if the refutation itself is instructive; apply corrected_claim text from adjusted verdicts; tag load-bearing claims [confirmed] / [adjusted] / [unverifiable]. Non-load-bearing findings keep their researcher confidence tag.)
## Product implications
(Directives for OUR product opinion and any missing requirements.)
## Sources
(Annotated links.)

RESEARCH (JSON):
${JSON.stringify(bundle.research)}

FACT-CHECK VERDICTS (JSON):
${JSON.stringify(bundle.verdicts)}

Your final message must be exactly the file path you wrote.`
}

function synthPrompt(files) {
  return `Read every NEW landscape dossier (files: ${files.join(', ')}) AND the existing technical research brief ${OUT_DIR}/00-research-brief.md and gap addendum ${OUT_DIR}/00b-gap-remediation-addendum.md (for scope context only — do not rehash their technical content). Then write a synthesis to ${OUT_DIR}/00c-product-landscape-brief.md using the Write tool.

${CONTEXT}

The brief is the bridge from landscape research to the PRODUCT OPINION. Structure:
# Product Landscape Brief — Events Concierge
## The landscape at a glance (category map: who plays where across discover -> rank -> register -> calendar -> reconcile; the one-line thesis of where the white space is)
## What incumbents cover and what none of them close (feature-coverage analysis from the commercial dossier)
## The horizontal-agent substitute threat (when a general agent is good enough, and the vertical wedge that survives it)
## What open source and indie builders keep proving and keep failing at
## The graveyard: why this category kills companies, and which failure modes our approach neutralizes vs still faces
## Trust & autonomy UX patterns to adopt (from adjacent concierge/scheduling products, with pricing anchors)
## Who the user is and what they will pay (demand evidence)
## Implications: positioning, wedge, and deltas to the signed-off requirements
## Open product questions for the owner
Max ~2800 words. Professional, dense, zero slop, no emoji. Cite dossier files by name so every claim is traceable.

Your final message (returned to the orchestrator, not shown to a human): a max-400-word executive summary of the brief.`
}

function criticPrompt(files) {
  return `You are a completeness critic for PRODUCT research. Read ${OUT_DIR}/00c-product-landscape-brief.md, then skim the landscape dossiers (${files.join(', ')}).

MISSION CONTEXT: ${CONTEXT}

Identify what is MISSING or WEAK in this landscape research before the owner forms a durable product opinion. Consider: competitor categories not swept (e.g., a major geography or channel missed); load-bearing market claims still unverifiable; demand/willingness-to-pay evidence too thin to price on; substitute threats unexamined; distribution/go-to-market channels unresearched; incumbent-response risk (what happens when Luma/Meetup ships this feature) unassessed; missed open-source building blocks. Do NOT list nice-to-haves — only gaps that would change the product opinion, positioning, or requirements. Return via schema.`
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
    const num = String(i + 18).padStart(2, '0')
    const path = `${OUT_DIR}/${num}-${dim.key}.md`
    return agent(writePrompt(dim, path, bundle), { label: `write:${dim.key}`, phase: 'Write', effort: 'low' }).then(() => path)
  }
)

const files = results.filter(Boolean)
log(`${files.length}/${DIMS.length} landscape dossiers written to ${OUT_DIR}`)

const summary = await agent(synthPrompt(files), { label: 'synthesize:landscape-brief', phase: 'Synthesize', effort: 'xhigh' })
const gaps = await agent(criticPrompt(files), { label: 'critic:product-completeness', phase: 'Synthesize', schema: GAPS, effort: 'high' })

return { files, brief: OUT_DIR + '/00c-product-landscape-brief.md', summary, gaps }