export const meta = {
  name: 'ec-judge-panels',
  description: 'Judge panels on the 4 contested Events Concierge architecture forks (3 divergent proposers + adversarial judge each)',
  phases: [
    { title: 'Propose', detail: '3 divergent-bias proposals per fork' },
    { title: 'Judge', detail: 'adversarial verify-and-decide per fork' },
    { title: 'Write', detail: 'persist judgments to design/panels/' },
  ],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const REQ = WS + '/design/requirements.md'
const R = WS + '/research'

const PROPOSAL = { type: 'object', required: ['bias','summary','components','flows','invariants','failure_handling','constraints_satisfied','constraints_traded','numbers','risks'], properties: {
  bias: { type: 'string' },
  summary: { type: 'string', description: 'The architecture in 3-6 sentences' },
  components: { type: 'array', items: { type: 'object', required: ['name','responsibility'], properties: { name: { type: 'string' }, responsibility: { type: 'string' }, tech: { type: 'string' } } } },
  flows: { type: 'array', items: { type: 'string' }, description: 'Concrete end-to-end data/control flows, step by step' },
  invariants: { type: 'array', items: { type: 'string' } },
  failure_handling: { type: 'array', items: { type: 'string' } },
  constraints_satisfied: { type: 'array', items: { type: 'object', required: ['constraint','how'], properties: { constraint: { type: 'string' }, how: { type: 'string' } } } },
  constraints_traded: { type: 'array', items: { type: 'object', required: ['constraint','why'], properties: { constraint: { type: 'string' }, why: { type: 'string' } } } },
  numbers: { type: 'array', items: { type: 'object', required: ['quantity','value','basis'], properties: { quantity: { type: 'string' }, value: { type: 'string' }, basis: { type: 'string' } } } },
  risks: { type: 'array', items: { type: 'string' } } } }

const JUDGMENT = { type: 'object', required: ['decision','rationale','mechanism','rejected_alternatives','salvaged_ideas','open_risks','edge_cases'], properties: {
  decision: { type: 'string', description: 'The decided architecture in 2-4 sentences, named' },
  rationale: { type: 'string', description: 'Why, with mechanism and numbers, grounded in requirements + dossier evidence' },
  mechanism: { type: 'array', items: { type: 'string' }, description: 'The decided concrete mechanism: components, flows, invariants, parameters' },
  rejected_alternatives: { type: 'array', items: { type: 'object', required: ['name','reason'], properties: { name: { type: 'string' }, reason: { type: 'string' } } } },
  salvaged_ideas: { type: 'array', items: { type: 'string' }, description: 'Ideas grafted from losing proposals' },
  open_risks: { type: 'array', items: { type: 'string' } },
  edge_cases: { type: 'array', items: { type: 'string' }, description: 'Edge cases the decided design must handle, each with its handling' },
  owner_ratification: { type: 'array', items: { type: 'string' }, description: 'Launch parameters or posture calls in this decision that the owner must ratify' } } }

const COMMON = [
  'PROJECT: Events Concierge - a multi-tenant product: natural-language request -> discover events across sources -> rank to user taste -> autonomously RSVP where the source permits (the honest split) -> write to Google Calendar with dedup behind a freeBusy conflict gate -> reconcile on organizer cancel/reschedule and user un-RSVP.',
  'LAUNCH POSTURE (HARD owner decisions): free RSVP only (paid deferred, payment port stubbed); API-first, browser best-effort off-SLA; per-source matrix: Meetup register = API-only for groups the user is ALREADY in (on SLA), Luma = browser best-effort, Eventbrite = discovery-only via SerpApi funnel with register handoff, Partiful = disabled, Ticketmaster = discovery-only via shared catalog crawl; post-booking lifecycle in v1; two SLA classes (autonomous lane vs handoff lane); UI-less launch with contract-specified touchpoints.',
  'SETTLED (do not re-litigate): Temporal Cloud is the durable spine (DBOS documented fallback, FR-8.1/O-6); credentials = self-built KMS-envelope vault + non-LLM injection broker + microVM isolation (d14); login codes = per-user RelayInbox email (d16); central read-through catalog cache for shared-app-key sources (d15); ranking = pgvector hybrid RRF + cross-encoder + LightGBM (d07). Your fork decides HOW these compose, not WHETHER.',
  'READ FIRST, in this order: (1) ' + REQ + ' - the BINDING signed requirements v0.2; every FR/NFR/AC cited below is defined there. (2) ' + R + '/00b-gap-remediation-addendum.md - wave-2 corrections that override wave-1 where they conflict. (3) The fork-specific dossiers listed below.',
].join('\n\n')

const ITEMS = [
  {
    n: 1, key: 'discovery-topology', title: 'Discovery and catalog-ingestion topology',
    dossiers: ['03-event-source-landscape.md','11-source-tier-reverify-2026.md','15-capacity-quota-model.md','07-ranking-personalization.md','08-clean-architecture-agentic.md'],
    question: 'How do the discovery surfaces compose into one concrete topology: the scheduled geo/category-sharded Ticketmaster catalog crawl, the SerpApi google_events cross-source funnel, per-user OAuth discovery (Meetup, Luma), ACL normalization to schema.org/Event, cross-source dedup into CanonicalEvents, and the pgvector/tsvector hybrid index? Decide: the store(s) of record and serving index (one Postgres vs staged pipeline vs federation); batch vs streaming ingestion shape; the crawl shard/cadence schedule that fits BOTH the 5,000-calls/day Ticketmaster budget AND the 6h freshness target (open item O-8 - show the arithmetic); how per-user discovery results merge with the shared tenant-neutral catalog at query time without violating tenant isolation; and where dedup runs (ingest-time vs query-time).',
    constraints: [
      'FR-3.3 + NFR-4a: shared-app-key sources are served from a central read-through catalog cache; an added tenant adds ZERO live Ticketmaster calls (AC-21).',
      'FR-3.4: Ticketmaster crawl respects 5 rps, 5,000 calls/day, size x page < 1000 - partition by facets, never deep-page.',
      'FR-3.5: Meetup/Luma discovery always under the requesting user own stored token, never shared.',
      'FR-3.6: SerpApi google_events runs as cross-source top-of-funnel, deduped across providers before per-source enrichment.',
      'FR-3.7 + FR-3.8: every CandidateEvent normalizes through an ACL onto schema.org/Event, prefering JSON-LD; dedup into CanonicalEvents happens BEFORE ranking; stable canonical_event_id; ALL per-source registration URLs retained on the merged record (AC-17).',
      'NFR-1: newly-published event discoverable within 6h [owner target]; interactive EventRequest returns ranked list p95 <= 5s served from cache + pgvector.',
      'FR-4.1: retrieval = pgvector dense ANN + Postgres tsvector/BM25 fused by RRF k~60 into 100-200 candidates.',
      'NFR-7: zero cross-tenant leakage - the catalog is tenant-neutral shared data; per-user preferences/requests/lifecycle are RLS-scoped.',
      'FR-3.9 + FR-10.1: automation_allowed per (source, modality) is DATA evaluated at call time; flipping it removes the source-modality within one config-reload, no deploy.',
    ],
    biases: [
      { name: 'single-Postgres pragmatist', stance: 'One Postgres cluster is the store of record AND the serving index: catalog tables + pgvector + tsvector + RLS-scoped per-user data in one system. Crawlers and the SerpApi funnel are plain workers upserting rows. Minimize moving parts, one backup story, one query surface; boring technology wins at 100k users.' },
      { name: 'staged-pipeline separation', stance: 'Ingestion is a pipeline with explicit stages: raw landing (per-source payloads) -> ACL normalize -> dedupe/merge -> publish to a serving index. Stages are replayable and idempotent; raw is kept for re-normalization; the serving store is a projection you can rebuild. Argue for the operational win: re-run dedup when the algorithm improves, audit provenance, absorb source-schema drift without data loss.' },
      { name: 'query-time federation minimalist', stance: 'Keep the central cache as thin as legally/quota required (Ticketmaster only); everything else federates at request time against live source APIs under the user own token, with short-TTL per-user result caches. Freshness beats staleness; storage and pipeline machinery are liabilities; the catalog is an optimization, not an architecture.' },
    ],
  },
  {
    n: 2, key: 'registration-orchestration', title: 'Registration orchestration and honest-split lane routing',
    dossiers: ['13-durable-execution-engine.md','17-meetup-rsvp-prerequisite-chain.md','10-per-source-legal-tos.md','04-autonomous-action-safety.md','15-capacity-quota-model.md'],
    question: 'What is the durable-orchestration shape from EventRequest intake to terminal state? FR-8.1 fixes one workflow per (user_id, canonical_event_id) - but FR-5.0 selection and fall-through (up to 3 candidate attempts per EventRequest) operates ABOVE that granularity. Decide: what owns EventRequest-level orchestration (parse -> discover -> rank -> select -> attempt loop) - a parent request-workflow spawning per-event child workflows, one flat workflow, or stateless services that only start per-event sagas; where the lane router lives and its exact decision function over (source, modality, group-condition); where the policy engine is evaluated and how the kill-switch reaches in-flight workflows (signal vs per-activity check); the saga step/compensation layout including read-before-mutate (Meetup) and detect-then-submit (browser) placement; idempotency-key minting; and the Meetup fair-share scheduler shape carried as the G2 contingency (per-app quota case).',
    constraints: [
      'FR-8.1: one durable workflow per (user_id, canonical_event_id), workflowId = user:event, reject-duplicate reuse policy. Temporal Cloud primary - settled.',
      'FR-5.0: register the single highest-ranked candidate passing conflict gate + hard constraints; fall through to next-ranked on failure, <= 3 attempts [owner target]; multi-source CanonicalEvent prefers autonomous-on-SLA -> browser-best-effort -> handoff; terminal = handoff task if any candidate handoff-eligible else failed_no_candidate + notification.',
      'FR-5.1/5.2/5.4/5.6: deterministic lane routing per (source, modality, group-condition); Meetup member-groups only via createEventRsvp under the user own token; Luma browser best-effort; Eventbrite/Partiful never autonomous.',
      'FR-5.3 + FR-5.5: Meetup read-RSVP-state-before-mutate; browser detect-then-submit with AMBIGUOUS -> human review, never blind re-POST.',
      'FR-5.9 + FR-7.2: policy engine (automation_allowed, per-user limits, kill-switch) evaluated before EVERY register activity; kill-switch engage freezes in-flight actions without a deploy (AC-40, AC-50).',
      'FR-8.2/8.3: saga discover->rank->select->resolve_membership->register_or_rsvp->await_confirmation->dedupe_calendar->write_to_calendar; at-least-once activities with compensation; idempotency keys minted ONCE in workflow state.',
      'FR-8.4: confirmation waits = durable timer + signal, timeout -> handoff. FR-8.5: claim-check for >2MB payloads. FR-8.9: transactional outbox for lifecycle events.',
      'FR-10.4 + G2 contingency: fair-share scheduler keyed (source, credential) enforcing Meetup 500 points/60s, Luma 100 POST/5min, TM 5 rps; if the Meetup quota proves per-app, degrade to global fair-share queue + handoff, no fixed SLA.',
      'NFR-8: exactly-once observable effects under crash injection (AC-56). NFR-15: every failure fails safe to handoff, never an SLA breach or ToS-violating action. NFR-2: handoff task available p95 <= 60s from routing decision.',
    ],
    biases: [
      { name: 'Temporal maximalist', stance: 'Everything durable lives in Temporal: a parent EventRequest workflow runs parse/discover/rank as activities, then spawns per-event child workflows for attempts; the attempt loop, lane routing, policy checks, fair-share pacing, and kill-switch are all workflow logic + signals. One execution model, full history, replay-debuggable, no bespoke state machines outside the engine.' },
      { name: 'thin-workflow fat-services', stance: 'Temporal only where durability pays: the per-event register saga. EventRequest intake/parse/discover/rank/select run in ordinary stateless services (they are read-only and idempotent, re-runnable on failure); the lane router and policy engine are libraries/services the saga calls; pacing is a shared token-bucket service. Keep the workflow surface small, testable, and cheap - workflow code is the hardest to change (versioning) so put the least logic there.' },
      { name: 'event-driven choreography', stance: 'The transactional outbox + a broker are the spine BETWEEN stages: intake, discovery, ranking, selection each emit events; small per-event Temporal workflows subscribe only for the side-effecting register/confirm/calendar leg; the lifecycle is the event log. Loose coupling, independent scaling, natural audit trail; policy and kill-switch are consumer-side gates.' },
    ],
  },
  {
    n: 3, key: 'credential-isolation', title: 'Credential, identity and browser-worker isolation topology',
    dossiers: ['14-credential-injection-worker-isolation.md','05-multitenancy-auth-secrets.md','16-email-ingestion-architecture.md','02-autonomous-browser-registration.md','15-capacity-quota-model.md'],
    question: 'Compose the settled security primitives into a deployable topology: the siloed KMS-envelope credential vault, the non-LLM injection broker, the per-(user,event) ephemeral microVM browser fleet, the mandatory egress proxy, the CaMeL dual-LLM boundary, and the RelayInbox OTP signal path. Decide: where the injection broker runs and its trust boundary (in-VM sidecar vs host daemon vs separate service) and its exact interaction with the Computer-Use reasoning loop (placeholder protocol, domain-pin check, zeroization points); who owns the browser fleet at launch and at 100k users (managed vendor vs self-hosted Firecracker/gVisor from day one) given the ~135-peak-concurrency sizing and the 100-session Browserbase Startup ceiling; where the reasoning loop runs relative to the VM (screenshots/DOM out, actions in); worker identity for session-cookie reuse (FR-2.4 same-worker-identity rule); and how the quarantined page-reading LLM is structurally separated from the action/injection path.',
    constraints: [
      'FR-2.2: KMS envelope encryption, pooled CMK, per-tenant DEK, tenant_id + credential_type as encryption context; vault siloed (own store, key hierarchy, network segment).',
      'FR-2.5: broker is non-LLM: placeholders in the reasoning loop, DEK unwrap, mlock + MADV_DONTDUMP buffer, CDP domain-pinning against the credential bound origin, Playwright fill(), zeroize immediately (AC-6, AC-7).',
      'FR-2.6 + NFR-6: plaintext secrets structurally excluded from prompts, tool args, Temporal history, logs - enforced by construction, verified by CI secret-scan.',
      'FR-2.7: one ephemeral Firecracker/gVisor microVM per (user, event), no cross-tenant coresidency, egress allowlist = target event origin + KMS endpoint only (AC-13).',
      'FR-7.4: CaMeL dual-LLM boundary - untrusted page content and ingested OTP/magic-link tokens never reach the credential-injection or action-decision path (AC-52).',
      'NFR-4b: pool provisioned to ~135 peak concurrent at 100k users (Friday peak, d15); exceeds Browserbase Startup 100-session ceiling -> Scale 250+ or self-host at the ~100-concurrent threshold; saturation fails over to handoff (FR-6.4), never an SLA breach.',
      'FR-2.4: session-cookie reuse only from the same worker identity; short-TTL cookie preferred over stored password (AC-12).',
      'FR-5.7/5.8: RelayInbox OTP/magic-link deterministically extracted (no LLM sees raw email), delivered as a durable workflow signal, treated as a short-TTL secret.',
      'NFR-5: browser lane <= $0.40 per confirmed RSVP - your topology must state its per-session cost basis.',
      'FR-2.8: login failure / MFA / CAPTCHA -> needs_reauth + halt + handoff, never retry loops.',
    ],
    biases: [
      { name: 'self-host security purist', stance: 'Own the whole fleet from day one: Firecracker microVMs on our EC2 bare-metal, our egress proxy, our CDP control plane. No third-party browser vendor ever sees a user credential or session; the isolation contract is enforced by OUR hypervisor boundary, not a vendor ToS. The 100-session vendor ceiling proves vendors cannot follow where we are going; build once, at launch scale (~10-15 concurrent), grow linearly.' },
      { name: 'managed-vendor pragmatist', stance: 'Launch on a managed browser pool (Browserbase Startup, then Scale 250+) with compensating controls: broker stays in OUR infra, credentials injected over CDP into vendor sessions pinned per (user,event), vendor contexts never persisted, egress rules via vendor policy. Self-hosting Firecracker is a distraction before product-market fit; revisit at the ~100-concurrent threshold with real usage data. Ship the product, not a hypervisor practice.' },
      { name: 'minimal-TCB capability broker', stance: 'Design around the smallest trusted computing base: a tiny broker process (hundreds of lines, formally reviewable) holds the only KMS decrypt capability; per-session ephemeral DEK grants; gVisor (not full VMs) for density and 125ms starts; everything else - reasoning loop, page LLM, even the orchestrator - is untrusted by construction and communicates through typed, capability-scoped channels. Argue the security economics: audit the 500 lines, not the fleet.' },
    ],
  },
  {
    n: 4, key: 'handoff-lifecycle', title: 'Human-handoff, lifecycle and organizer-change-detection subsystem',
    dossiers: ['16-email-ingestion-architecture.md','17-meetup-rsvp-prerequisite-chain.md','06-calendar-integration.md','13-durable-execution-engine.md','11-source-tier-reverify-2026.md'],
    question: 'Design the first-class handoff + lifecycle + change-detection subsystem for the UI-less launch. Decide: where the per-(user,event) lifecycle state machine lives (Temporal workflow state as source of truth with DB projections, DB state machine with workflows as executors, or an event-sourced log); the handoff queue concretely (store, task states, TTL machinery, deep-link construction, the p95<=60s task-available and p95<=120s user-notified paths); the launch notification channel choice (email to real address vs SMS) with at-least-once + retry + dedup mechanics; completion-confirmation matching (RelayInbox confirmation email vs source webhook vs user mark-done - how a completion is MATCHED to its task) plus the freeBusy re-check at completion; the organizer-change-detection topology - one detection service vs per-source pollers vs extending the catalog crawl vs per-workflow polling timers - covering TM crawl-delta, Meetup poll, Luma poll/RelayInbox-email, and handoff-lane events on the 6h cadence; and how detections signal per-(user,event) workflows.',
    constraints: [
      'FR-6.1: per-user handoff queue; each task carries CanonicalEvent, reason enum, policy TTL, deep link landing as close to one-tap as the source allows.',
      'NFR-2(b): time-to-handoff-task-available p95 <= 60s AND time-to-user-notified p95 <= 120s from routing decision - delivery, not enqueue.',
      'FR-6.6: launch notification channel = email to the user real address or SMS; at-least-once, bounded retry, dedup; RelayInbox is inbound-only, never outbound; task past TTL [owner target <= 7 days] -> expired, closes workflow, notifies user (AC-46).',
      'FR-6.3: completion signal (RelayInbox confirmation email, source webhook, or explicit mark-done) re-runs the freeBusy gate at completion time; conflict -> warning; else lifecycle -> registered (AC-43).',
      'FR-6.7 + FR-6.8: inbound un-RSVP channel and EventRequest intake channel (email/SMS/API) are launch contracts - the UI is deferred, the contracts are not.',
      'Lifecycle: found -> registered -> scheduled -> reconciled, plus handoff and un-RSVP branches, plus terminal completed / cancelled / expired / failed_no_candidate; EVERY workflow ends in exactly one terminal state and releases resources.',
      'FR-8.7 + FR-8.7a: organizer cancel/reschedule detected via webhook where offered else scheduled re-poll on NFR-17 cadence [<= 6h]; per-source modality: TM = catalog-crawl delta, Meetup = poll, Luma = poll or RelayInbox change-email, handoff-lane events (registered outside our surface) = poll or RelayInbox email; normalize to schema.org eventStatus; reconcile updates/removes calendar entry + notifies (AC-60, AC-61).',
      'FR-8.8: un-RSVP withdraws where source permits (else handoff), removes calendar entry, records transition - no payment path.',
      'FR-8.9: lifecycle transitions persist via transactional outbox, at-least-once with idempotent consumers (AC-63).',
      'FR-6.4 + NFR-15: degraded browser/Meetup paths fail over to handoff and never cascade into the autonomous-lane SLA.',
    ],
    biases: [
      { name: 'workflow-native purist', stance: 'The per-(user,event) Temporal workflow IS the lifecycle: handoff tasks are just a workflow phase with a durable timer (TTL) waiting on a completion signal; change detection delivers signals into the workflow; the handoff queue and notification sends are activities; DB tables are read-only projections for queries. One source of truth, zero state-machine drift, the engine already solved timers/retries/dedup.' },
      { name: 'product-subsystem pragmatist', stance: 'Handoff tasks, notifications, and lifecycle rows are a conventional product subsystem: Postgres tables with state columns, a notifier worker with an outbox, per-source poller cron jobs, webhook endpoints. Workflows call INTO it and subscribe to its events but the product surface (queues a future UI will render, notification history, task lists) must be queryable/ownable without replaying workflow histories. Product data belongs in product tables.' },
      { name: 'event-sourced CDC separation', stance: 'Lifecycle is an append-only event log (the outbox IS the log): every transition is an event; handoff queue, notifications, calendar reconcile, and analytics are independent idempotent consumers; change detection is a dedicated diffing service emitting normalized eventStatus deltas onto the same log. Replayable, auditable end-to-end (NFR-10 falls out for free), each consumer scales/fails independently.' },
    ],
  },
]

function proposePrompt(item, bias) {
  return [
    'You are a staff engineer writing ONE architecture proposal for a contested fork of the Events Concierge system design. Argue YOUR assigned bias as its strongest self - a genuinely convinced, technically rigorous advocate. You are competing against two other proposals from different biases; an adversarial judge will verify every claim.',
    COMMON,
    'FORK-SPECIFIC DOSSIERS (read these): ' + item.dossiers.map(d => R + '/' + d).join(' , '),
    'THE FORK: ' + item.question,
    'BINDING CONSTRAINTS - satisfy each, or explicitly list it in constraints_traded with an honest why:\n- ' + item.constraints.join('\n- '),
    'YOUR BIAS: ' + bias.name + ' - ' + bias.stance,
    'Rules: mechanism must be CONCRETE - named components with responsibilities, step-by-step flows, invariants, failure handling, and numbers with a stated basis (dossier line or arithmetic you show). No hand-waving, no it-depends. Where your bias genuinely cannot satisfy a binding constraint, say so honestly in constraints_traded rather than papering over it. Do not re-litigate settled decisions. Return the proposal via the structured output tool.',
  ].join('\n\n')
}

function judgePrompt(item, proposals) {
  return [
    'You are an adversarial judge deciding ONE contested architecture fork for the Events Concierge system design. Three proposals argue deliberately divergent biases. Your job: VERIFY their claims against the binding requirements and the research dossiers YOURSELF - do not trust any proposal self-assessment - then attack each proposal at its weakest binding constraint, then DECIDE. A graft (winning base + salvaged ideas from losers) is often the right verdict. Your output must be concrete enough that a design-doc deep dive (Problem -> Approaches -> Decision -> Rationale -> Edge cases) and an ADR can be written from it directly, without going back to the proposals.',
    COMMON,
    'FORK-SPECIFIC DOSSIERS (read these): ' + item.dossiers.map(d => R + '/' + d).join(' , '),
    'THE FORK: ' + item.question,
    'BINDING CONSTRAINTS (the decided design must satisfy every one, or you must explicitly flag the exception as an owner-ratification item):\n- ' + item.constraints.join('\n- '),
    'THE THREE PROPOSALS:\n' + JSON.stringify(proposals, null, 1),
    'Rules: check the proposals numbers against the dossiers (stale or invented numbers are the most common defect); reject with REAL reasons, not straw men; make the decided mechanism concrete (components, flows, invariants, launch parameters); list edge cases WITH their handling; flag anything the owner must ratify. Return via the structured output tool.',
  ].join('\n\n')
}

function writePrompt(item, bundle) {
  const path = WS + '/design/panels/0' + item.n + '-' + item.key + '.md'
  return [
    'Write the file ' + path + ' rendering this judge-panel result as clean markdown. Structure: H1 = Panel ' + item.n + ': ' + item.title + '; then sections: The fork (the question verbatim); Decision; Rationale; Decided mechanism (bullet list); Rejected alternatives (name + reason); Salvaged ideas; Edge cases; Open risks; Owner ratification items; then a final section Proposals (for the record) with each proposal bias + its summary field only. Dense prose, no emoji, no filler. Use the JSON below verbatim as the content source - do not invent or embellish.',
    'JUDGMENT JSON:\n' + JSON.stringify(bundle.judgment, null, 1),
    'PROPOSAL SUMMARIES JSON:\n' + JSON.stringify(bundle.proposals.map(p => ({ bias: p.bias, summary: p.summary })), null, 1),
    'Return only the file path when written.',
  ].join('\n\n')
}

phase('Propose')
const results = await pipeline(ITEMS,
  async (item) => {
    const proposals = (await parallel(item.biases.map((b, i) => () =>
      agent(proposePrompt(item, b), { label: 'propose:' + item.key + ':' + (i + 1), phase: 'Propose', schema: PROPOSAL, effort: 'high' })
    ))).filter(Boolean)
    log('panel ' + item.key + ': ' + proposals.length + '/3 proposals in')
    return proposals.length ? proposals : null
  },
  async (proposals, item) => {
    if (!proposals) return null
    const judgment = await agent(judgePrompt(item, proposals), { label: 'judge:' + item.key, phase: 'Judge', schema: JUDGMENT, effort: 'xhigh' })
    if (!judgment) return null
    log('panel ' + item.key + ': judged - ' + (judgment.decision || '').slice(0, 140))
    return { proposals, judgment }
  },
  async (bundle, item) => {
    if (!bundle) return null
    await agent(writePrompt(item, bundle), { label: 'write:' + item.key, phase: 'Write', effort: 'low' })
    return { key: item.key, n: item.n, title: item.title, judgment: bundle.judgment }
  }
)

const judgments = results.filter(Boolean)
log('panels complete: ' + judgments.length + '/4 judgments written to design/panels/')
return { judgments }