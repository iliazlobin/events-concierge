# Research Brief — Events Concierge (multi-tenant, autonomous)

Research snapshot from the original full-concierge design. Preserve its source dates and evidence; [PROJECT.md](../PROJECT.md#current-milestone-private-discovery-candidate) defines current scope, and [release gates](../docs/operations/release.md#first-release-acceptance) define activation.

> Synthesis of the nine research dossiers (`01`–`09`) into the load-bearing evidence for the design
> phase. Every claim cites the dossier it comes from. The 2026 Claude-stack "thesis" is validated
> where the evidence supports it and explicitly corrected where it does not.

## What we are building

A shareable, multi-tenant product where a user issues a natural-language request ("find me something
Friday evening after work and sign me up") and the system **discovers** candidate events across many
sources, **ranks** them to that user's taste and hard constraints, autonomously **registers/RSVPs**
(including paid tickets) on the event's own site, and writes the confirmed event to the user's
**calendar** with de-duplication — tracking each event through a `found → registered →
scheduled_to_calendar` lifecycle. Autonomy is total (no per-action human confirmation); safety lives in
configured policy, spending limits, idempotency, and auditability. Because agents act on *other users'*
accounts and money, authorization, tenant isolation, and credential security are first-class, not
afterthoughts.

## Architecture direction the evidence supports

**Orchestration — split macro control-flow from micro reasoning.** The 2026 consensus is that
orchestration moved *out* of the model's conversation and into external code (`01-agent-orchestration-2026`).
The Claude Agent SDK "session" persists only the conversation transcript; its default store is documented
as *not* production-safe, and checkpoint/resume replays side effects — a transcript resume alone can
double-charge (`01-agent-orchestration-2026`). The direction: put the `found → registered →
scheduled_to_calendar` state machine in an **external durable-execution engine** (Temporal or equivalent),
one durable workflow instance per `(tenant_user, event)`, with `workflowId = {user_id}:{event_id}` as the
idempotency key; wrap each irreversible sub-step (submit, charge, confirm, calendar-write) as an idempotent
Activity with retry + compensation; model "registration needs email confirmation" as a durable timer +
signal, not a blocking agent turn (`01-agent-orchestration-2026`). The Claude native tool-use loop owns
reasoning *inside* each Activity; Agent SDK subagents provide **least-privilege decomposition** (a discovery
subagent scoped to read/search, a per-source register subagent scoped to that source's tools + payment, a
calendar subagent) — not durability (`01-agent-orchestration-2026`, `08-clean-architecture-agentic`).

**Clean architecture — LLM as a driven port.** Treat the agent as the domain core's *reasoning engine*
and a driven port, with tools as capability adapters; every exposed tool schema is billed per turn, so port
surfaces must be minimal (`08-clean-architecture-agentic`). Declare **one `SourcePort`** with
`discover()` and `register()`, implemented twice per source — an API adapter (preferred) and a browser
adapter (fallback) — with deterministic strategy selection on a per-source capability flag, never model
choice (`08-clean-architecture-agentic`, `03-event-source-landscape`). Normalize every vendor schema
through an Anti-Corruption Layer onto **schema.org/Event** as the canonical vocabulary; prefer parsing a
page's existing JSON-LD `Event` block over DOM scraping (`08-clean-architecture-agentic`,
`03-event-source-landscape`). Persist lifecycle transitions via **transactional outbox** (Debezium CDC, not
polling), keep domain state in relational tables, and append an immutable business-fact action log for
audit (`08-clean-architecture-agentic`). Instrument with OpenTelemetry GenAI conventions
(`gen_ai.input.messages`/`output.messages`, not the deprecated `gen_ai.prompt`/`completion`), tagging every
span with `tenant_id` (`08-clean-architecture-agentic`).

**Source tiering — three explicit bands.** The source landscape forces a tier list encoded in the adapter
layer (`03-event-source-landscape`): (A) **clean-search API** — Ticketmaster Discovery v2 (also surfaces
Universe), SeatGeek (partner-gated), Bandsintown/Songkick (read-only, discovery only); (B) **management-only
or Pro-gated API + browser RSVP** — Meetup (GraphQL, Pro, RSVP mutation possible, ~200 req/hr), Luma (Plus,
management-only, RSVP via browser); (C) **browser-only for both discovery and registration** — Eventbrite
discovery (public event *search* was killed in 2019; only ID/venue/org lookup survives), Luma registration,
Partiful, Resident Advisor, Meta Events. A SerpApi `google_events` sweep is a cheap cross-source
top-of-funnel that de-dups across providers before per-source enrichment (`03-event-source-landscape`).

**Browser automation — DOM skeleton, vision-CUA fallback; NOT the Chrome extension.** The Claude-in-Chrome
*extension* forces purchases, account creation, and financial actions through a mandatory interactive
approval that cannot be disabled in any mode — functionally a block on exactly the actions we need, so it
**cannot be the autonomous paid-registration engine** (`02-autonomous-browser-registration`). Use the
programmatic **Computer Use API** (beta header `computer-use-2025-11-24`) and/or a DOM-driven harness
(Playwright/CDP, Stagehand-style), preferring scripted DOM adapters per source and falling back to vision-CUA
only for API-less/JS-heavy/invite-only sites (Luma, Partiful) (`02-autonomous-browser-registration`). Realistic
per-attempt success is ~56–61% general / ~64% on state-mutating "write" tasks that resemble checkout — *not*
the inflated 85–94% WebVoyager numbers — so engineer for that ceiling with mandatory pre-submit verification
(re-screenshot + confirm price/date/fields), a single idempotency key per `(user, event)`, and a policy-gated
retry budget (`02-autonomous-browser-registration`). Ticketing sites are actively hostile (Akamai/Cloudflare/
DataDome, Verified Fan, World ID); Ticketmaster ToS + the federal **BOTS Act** make automated *purchase* on
the TM/Live Nation family legally hazardous — discovery is fine, checkout is not — so paid autonomy must be a
per-source policy flag, disabled by default on TM-family, never evasion; degrade to human handoff on
CAPTCHA/identity walls (`02-autonomous-browser-registration`, `03-event-source-landscape`).

**Ranking — two-stage hybrid, not a single index.** Replace a single-index scorer with hybrid retrieval
(pgvector dense ANN + Postgres `tsvector`/BM25) fused by **Reciprocal Rank Fusion (k≈60)** to ~100–200
candidates, then a **cross-encoder rerank** (Cohere Rerank v3.5, ~$2/1k searches) — behind one swappable
`Ranker` port (`07-ranking-personalization`). An Opus/Sonnet listwise pass buys only ~0.04 NDCG at ~9× cost
and ~35× latency, so reserve it for a final top-10 diversity/clash tie-break, not the primary ranker
(`07-ranking-personalization`). **Postgres + pgvector is the single store** for events, embeddings, users,
preferences, credentials, and lifecycle — a self-hosted OpenSearch/Elasticsearch cluster is not warranted for
a filter-heavy `<50M`-row corpus, and colocating simplifies tenant isolation via row-level security
(`07-ranking-personalization`). Fold personalization in as a feature-based re-scoring layer (LambdaMART/
LightGBM) over user↔event embedding cosine + explicit affinities + implicit attended/declined signals; seed
cold-start from onboarding attribute-preferences + content-based metadata + LLM test-time intent inference
(`07-ranking-personalization`). **De-dupe before ranking** (block by city/date, match on fuzzy title +
description-embedding cosine + time/geo delta, merge into one canonical event that *retains all per-source
registration URLs*) (`07-ranking-personalization`).

## Autonomy, authorization & payments posture

Guardrails are configured policy, not interactive prompts. Encode autonomy limits as **externalized
declarative policy** (OPA/Rego or Cedar) evaluated at the money-moving boundary: per-user spending caps
(per-event, daily, monthly), vendor/category allowlists, dry-run/monitor mode, and a data-plane
**kill-switch** (per-user and global) that freezes all money-moving tool calls without a deploy
(`04-autonomous-action-safety`). Model the **payment port on the OpenAI/Stripe Agentic Commerce Protocol
(ACP)** `delegate_payment`: mint a single-use vault token bound to one `merchant_id`, one `max_amount`
(set to the exact expected ticket price), currency, and short `expires_at` — never store or replay a raw
PAN (`04-autonomous-action-safety`). Every money-moving POST carries a deterministic `Idempotency-Key`
scoped to `(user_id, action, endpoint)`, first-response persisted, body-hash equality enforced on replay,
in-flight duplicates handled with `Retry-After` — mirroring ACP's exact conflict/replay semantics
(`04-autonomous-action-safety`, `08-clean-architecture-agentic`). Represent user delegation as a signed,
bounded consent artifact (AP2-style Intent Mandate: budget cap + vendor class + time window) signed once by
the user; in autonomous mode the agent signs the concrete purchase mandate against those constraints
(`04-autonomous-action-safety`). Design explicitly for the **3-D Secure / SCA gap**: an autonomous charge
cannot answer a step-up challenge, so route paid tickets through pre-authenticated network tokens
(cryptogram/ECI) or the site's stored payment, and degrade to a queued human-auth step on "challenge
required" (`04-autonomous-action-safety`). Model register→pay→calendar as an **idempotent saga**; order all
irreversible steps last; treat non-refundable tickets as a saga-pivot requiring forward-recovery/manual
review, not blind rollback (`04-autonomous-action-safety`, `08-clean-architecture-agentic`). The immutable
per-action audit log (signed consent, exact event/price/time, policy decision, idempotency key, vault token)
is the **primary chargeback-dispute evidence** — under 2026 rules the merchant/operator is liable by default
(`04-autonomous-action-safety`).

## Multi-tenancy, auth & credential-security posture

Adopt a **bridge isolation topology**: pool user/profile/preference/lifecycle data behind Postgres RLS,
but **silo the third-party-credential vault** (own DB, KMS key hierarchy, network segment, IAM boundary) —
it is the highest-value blast target (`05-multitenancy-auth-secrets`). RLS mechanics are load-bearing:
`FORCE ROW LEVEL SECURITY`, connect as a non-owner role without `BYPASSRLS`, set tenant context with
`SET LOCAL` inside each transaction (pgBouncer transaction-mode safe), and index `tenant_id` as the leading
column — or get silent leakage / ~100× slowdowns (`05-multitenancy-auth-secrets`). Encrypt every secret with
**KMS envelope encryption**, pooled CMK + per-tenant DEK, passing `tenant_id` + `credential_type` as KMS
encryption context so a decrypt for tenant A cannot read tenant B — which also buys GDPR Art. 34 / CA AB 1130
breach-notification safe harbor while keys stay in KMS separate from ciphertext
(`05-multitenancy-auth-secrets`). Never store a raw third-party password when OAuth exists; for password/
cookie sites (Luma, Partiful) true zero-knowledge is impossible because the agent needs plaintext at
runtime, so decrypt only in-memory inside the browser worker, never log, never place in an LLM prompt/tool-arg/
transcript, zeroize after use, and prefer short-TTL session cookies over long-lived passwords
(`05-multitenancy-auth-secrets`, `02-autonomous-browser-registration`). Adopt the **1Password Secure Agentic
Autofill / Browserbase** pattern (Noise-framework E2E channel, minimal-field injection, TOTP at runtime) so
neither the LLM nor the agent process sees the secret — swapping its per-request human approval for
policy-based auto-approval scoped to the login domain (`02-autonomous-browser-registration`). Product auth =
**OIDC + Backend-for-Frontend**: browser holds only an httpOnly/Secure/SameSite session cookie, tokens stay
server-side, `tenant_id` is a signed claim resolved once at the request edge and threaded into RLS
(`05-multitenancy-auth-secrets`). Requesting Google Calendar/Gmail scopes forces **CASA Tier 2** (annual
third-party DAST) — a hard product gate; store exactly one refresh token per user per client to avoid the
~100-token silent-invalidation churn (`05-multitenancy-auth-secrets`).

## Calendar integration & dedup direction

Make calendar writes idempotent with a **deterministic client-set event id** (base32hex hash of `tenant`,
`source`, `sourceEventId`): insert, and on HTTP 409 `duplicate` switch to patch — a clean idempotent upsert
(`06-calendar-integration`). Stamp `extendedProperties.private.concierge_source = '<source>:<id>'` and
`reg_state` on every write, and query them back (`events.list?privateExtendedProperty=...`) as the primary
dedup key; layer a fuzzy secondary dedup (normalized title + start within ~15 min + venue token overlap) to
absorb human-added and cross-source duplicates, resolving matches as a merge/patch (`06-calendar-integration`).
Add a **mandatory pre-registration conflict gate**: call `freeBusy` over the candidate window across all the
user's calendars before any paid RSVP and abort/deprioritize on overlap — enforcing "never book two
overlapping events" without a prompt (`06-calendar-integration`). Run change ingestion as webhook-triggered
incremental sync (Google `syncToken` / Graph `deltaLink`), handle HTTP 410 by full resync, and schedule
weekly channel renewal (no auto-renew) (`06-calendar-integration`). One `CalendarPort` with Google, Microsoft
Graph (delta query, 1,000-subscription/mailbox cap), and Apple/iCloud CalDAV (app-specific password, VEVENT
UID for dedup) adapters; always set IANA `timeZone` (required for recurring, correct across DST) rather than
bare offsets (`06-calendar-integration`).

## Key facts, numbers & cost model for back-of-envelope

- **Model price ladder** (`09-cost-efficiency-scale`): Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15, Sonnet 5 intro
  $2/$10 (→$3/$15 Sep 1 2026), Opus 4.8 $5/$25, Fable 5 $10/$50 per MTok (in/out); output is 5× input.
  Opus 4.7+/Sonnet 5 use a newer tokenizer producing ~30% more tokens, so Opus-vs-Sonnet-4.6 effective cost
  is ~2.2×, not the sticker 1.67× — nudging the default reasoning model to Sonnet 4.6.
- **Registration unit economics** (`09-cost-efficiency-scale`, `02-autonomous-browser-registration`): an
  **API-path** registration is `<$0.05` (Haiku parse + field-map, few K tokens); a **browser-path**
  registration is ~$0.54 (Sonnet 4.6) / ~$0.90 (Opus 4.8) for a 25-step run, more with retries — a **20–60×
  gap** that makes API-first the primary cost strategy. Screenshots are the hidden sink: each accumulates
  ~1,334 tokens (1280×800) every turn; prune to the last 1–3 frames.
- **Browser infra is a rounding error**: ~$0.10–0.12/browser-hour (Browserbase), so a 3–5 min session is
  ~$0.005–0.010 — under 1–2% of the LLM cost; outsource until >~5,000 concurrent sessions
  (`02-autonomous-browser-registration`, `09-cost-efficiency-scale`). Managed Agents adds only $0.08/session-hr.
- **Prompt caching** cuts cache-read input to 0.1× *and* excludes it from the per-minute rate limit — a
  cost cut *and* a 5–10× throughput multiplier; **Batch API** is a flat 50% off and stacks with caching
  (~95% combined). Route discovery + ranking through Batch on Haiku/Sonnet; keep registration synchronous
  (`09-cost-efficiency-scale`).
- **Shared quotas are the scaling ceiling** (`09-cost-efficiency-scale`, `03-event-source-landscape`):
  Ticketmaster 5,000/day + 5 rps + 1,000-item deep-paging cap; Eventbrite ~2,000/hr; Meetup ~200/hr; Luma
  200/min-calendar; Anthropic limits are org-level (Tier 4 = 4,000 RPM / 2M ITPM). Prefer per-user stored
  credentials so quota draws against the user's account, behind a global fair-share scheduler.
- **Ranking/embeddings are cheap** (`07-ranking-personalization`): embedding 1M events ≈ $26 (3-large),
  Cohere rerank $2/1k; pgvector ~75% cheaper than Pinecone at 50M vectors.

## What we deliberately change from the 2025 prototype

- **Orchestration:** LangGraph StateGraph supervisor + `MemorySaver` → external **durable-execution engine**
  (Temporal-class) for the lifecycle, with a thin Claude native tool-use loop + MCP for reasoning. Rationale:
  checkpointing is not durable execution and can double-charge on resume (`01-agent-orchestration-2026`).
- **Browser automation:** AutoGen MultimodalWebSurfer → **DOM-driven harness + Computer Use API** with
  vision-CUA fallback. The 2026-thesis Claude-in-Chrome *extension* is **refuted** for the paid leg (mandatory
  approval gate) and reserved for interactive/dev use only (`02-autonomous-browser-registration`).
- **Discovery/index:** self-hosted OpenSearch `function_score` → **Postgres + pgvector hybrid RRF +
  cross-encoder rerank**; a separate search cluster is not warranted (`07-ranking-personalization`).
- **Reasoning model:** single `gpt-4o-mini` → **3-tier Claude routing** (Haiku for parse/extract/dedup,
  Sonnet 4.6 default register reasoning, Opus 4.8 only on hard/failed cases) as a per-step policy decision
  (`09-cost-efficiency-scale`).
- **Tools:** bespoke Python wrappers → **MCP servers** surfaced uniformly to the loop
  (`01-agent-orchestration-2026`, `08-clean-architecture-agentic`).
- **New because it is now a product, not a prototype:** bridge tenant isolation + siloed KMS credential
  vault, OIDC/BFF auth, ACP-style scoped payment tokens + policy-engine spending limits, saga + idempotency
  ledger, immutable audit log, and per-source paid-autonomy policy flags — none of which existed in the
  test-account prototype (`04-`, `05-`, `08-autonomous-action-safety`/`multitenancy-auth-secrets`/`clean-architecture-agentic`).

## Open questions for the design phase

1. **Durable engine choice** — Temporal vs Restate/DBOS/Inngest, and how the ~2MB payload cap forces bulky
   screenshots/DOM/event-lists into object storage referenced by key (`01-agent-orchestration-2026`).
2. **Payment rails sequencing** — do we ship ACP `delegate_payment` first and keep the Allowance/mandate
   abstraction generic for AP2 / Visa-Mastercard agent tokens later, and which merchants actually accept
   agent-tokenized payment at launch (`04-autonomous-action-safety`)?
3. **Paid-autonomy scope per source** — which sources get `paid_allowed=true` by default given ToS/BOTS-Act
   exposure; is paid checkout limited to free-RSVP + Luma/Partiful + general-web stored-credential flows at
   launch (`03-event-source-landscape`, `02-autonomous-browser-registration`)?
4. **Credential-injection integration** — 1Password Agentic Autofill dependency vs a self-built KMS-envelope
   injection worker, and how to auto-approve without the per-request human prompt (`02-`, `05-`).
5. **CASA Tier 2 timeline & scope minimization** — can calendar-only launch avoid Gmail scopes; incremental
   authorization at the add-to-calendar step; annual DAST budget (`05-multitenancy-auth-secrets`).
6. **Browser success-rate operations** — target step ceiling, retry budget, and the human-handoff UX for the
   ~36–44% of browser attempts that fail at the ~56–64% ceiling (`02-autonomous-browser-registration`).
7. **Personalization model lifecycle** — per-tenant LightGBM vs a shared model with per-user features, and
   the decaying blend from cold-start explicit prefs to implicit LTR (`07-ranking-personalization`).
8. **SLA & pricing** — set limits/pricing against the browser-heavy Opus worst case (~$1–3/confirmed event),
   not the API best case, to protect margin (`09-cost-efficiency-scale`).
