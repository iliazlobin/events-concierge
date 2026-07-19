# Clean Architecture and Reliability Patterns for an Agentic Events Concierge

> Defines where non-determinism, money-moving side-effects, and third-party schema chaos are allowed to live so an autonomous, multi-tenant concierge stays debuggable, reversible, and auditable.

## Verified findings

### The agent is a driven port, not the app [confirmed]
Reframe the hexagon center from a rules engine to a *reasoning engine*: the LLM/agent invokes capability ports (`EmailSenderPort.send_email`, `SearchPort.search`) while MCP servers, tool-calling interfaces, and REST clients are the concrete outbound adapters. Binding is by "semantic late binding, where meaning resolves at runtime via natural language rather than at compile time via function signatures." The non-classical discipline that must drive port design: "the cost of a bloated adapter surface is paid in tokens on every single request" — every tool schema exposed to the model is billed on each turn, so ports must be minimal. This isolates non-determinism inside an adapter boundary and lets you swap Opus/Sonnet, prompts, and guardrails while ports/adapters stay fixed. (Caveat: the second cited source, martia_es, actually places the LLM in the *infrastructure* layer and does not discuss token cost or late binding — it is a weak corroborator. The primary anoliphantneverforgets source confirms every load-bearing element verbatim. Note also that the pattern frames the LLM as the hexagon *center*; "and as a driven port" is a defensible but loose framing for an agent invoked behind a port within a larger app.)

### Anti-Corruption Layer onto schema.org/Event as canonical vocabulary [confirmed]
Route every messy third-party event schema through an ACL whose translator "converts between your domain model and the external model" so external concepts never leak into the core, mapping onto schema.org/Event with exact typed properties:
- `name` → Text
- `startDate`/`endDate` → Date or DateTime, ISO-8601
- `location` → Place | PostalAddress | Text | VirtualLocation
- `offers` → Offer (carries price + availability)
- `eventAttendanceMode` → Online/Offline/MixedEventAttendanceMode
- `eventStatus` → EventScheduled | EventRescheduled | EventCancelled | EventPostponed (schema.org also defines a 5th value, EventMovedOnline)
- `organizer` → Organization | Person; `url` → URL; `description` → Text; plus `performer`, `duration` (ISO-8601 Duration)

Google's event structured-data guidance uses this exact vocabulary and parses JSON-LD `@type:Event` markup, so many source sites already emit parseable JSON-LD Event blobs — prefer parsing those over raw DOM scraping. This is stable DDD + vocabulary knowledge with no volatility.

### Transactional outbox for reliable lifecycle + money-moving effects [confirmed]
Write domain state + an outbox row in ONE local DB transaction ("store the message in the database as part of the transaction that updates the business entities"), avoiding the dual-write problem. A relay publishes via Polling Publisher or Transaction Log Tailing. Delivery is at-least-once — "the Message relay might publish a message more than once... crash after publishing but before recording" it — so every consumer "must be idempotent, perhaps by tracking the IDs of the messages that it has already processed." Outbox preserves per-aggregate ordering. Polling adds latency and DB load; the current standard relay is CDC via Debezium 2.5+ tailing the WAL (Conduktor cites single-digit-ms latency). [adjusted] "2025 industry-standard fix" is a reasonable source-backed characterization rather than a hard fact.

### Stripe-style idempotency for every irreversible/paid action [confirmed]
Client-generated `Idempotency-Key` header (up to 255 chars, V4 UUID suggested) on all POST; the server "saves the resulting status code and body of the first request... regardless of whether it succeeds or fails," and subsequent requests with the same key return the same result, including 500s, within a 24-hour window (keys pruned after). Replays carry `Idempotent-Replayed: true`. brandur.org's Postgres-backed scheme persists (user_id, idempotency_key) uniqueness plus method/params/path, cached status+body, a `recovery_point`, and a `locked_at` lock (409 on concurrent replay).

Two implementation cautions [adjusted], not refutations:
1. Deriving a *deterministic* key = hash(user_id, event_id, action) departs from Stripe's suggestion of *random* UUIDs. It must be scoped so a user can never legitimately repeat the same logical action (re-RSVP after cancel; buy a 2nd ticket) — otherwise the second real request silently collapses onto the first's stale reply.
2. Persisting only `{status, body, external_ref}` captures completed-request replay but NOT brandur's crash-recovery behavior. Adopt the `recovery_point` + row lock so a crash mid-charge resumes rather than re-charges.

### Orchestrated saga with a compensation registry; irreversible steps last [high]
Compensations must be idempotent (retried on network failure) and "work on partial state." For truly irreversible actions (payment, calendar write, confirmation email) the recommended strategy is to "delay them to the end of the saga after all other steps have succeeded, so no compensation is needed if an earlier step fails." Orchestration (central coordinator sending commands) beats choreography for complex/debuggable flows. The RAC paper (Perera et al., ACM CAIS '26) formalizes a *compensation registry* — a structured action→rollback mapping — plus action-state tracking and forward recovery when full rollback is infeasible (non-refundable ticket → notify user / de-schedule calendar hold rather than un-buy). Temporal is the mature durable-execution engine for such long-running sagas with automatic retries and versioning.

### Outbox-over-tables as default; event sourcing only for the audit stream [high]
Transactional outbox "stores business data in traditional tables and uses events for inter-service communication" — right when you need high consistency between DB and broker on a relational store. Event sourcing makes the log the source of truth and reconstructs by replay, chosen "when auditability and reconstructability matter more than simple CRUD," but "demands strong event discipline. Events must be business facts, not vague technical log lines." Practical hybrid: keep domain state (User, EventRequest, Registration) in normal tables + outbox, and additionally append an immutable business-fact action-log stream (RegistrationAttempted/Charged/Confirmed) for autonomous-money-action auditability.

### OpenTelemetry GenAI semantic conventions for portable traces [high]
Span hierarchy: top-level `invoke_agent` span with child `chat` spans per LLM call and `execute_tool` spans per tool invocation (operations: chat, execute_tool, invoke_agent, create_agent). Attributes: `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.operation.name`, `gen_ai.system_instructions`, `gen_ai.input.messages`/`output.messages`, plus conversation/agent IDs. Currency: as of v1.38.0 (2026) `gen_ai.prompt` and `gen_ai.completion` are deprecated/removed — use `gen_ai.input.messages`/`gen_ai.output.messages`. Datadog, LangSmith-style tools, and OpenLLMetry consume these natively.

### Three-layer test harness for the agentic pipeline [high]
(1) Per-adapter recorded-HTTP (VCR-style) fixtures + provider contract tests so a source's schema change fails a test rather than silently corrupting the ACL. (2) Golden-transcript *trajectory* evals (~30 cases) gating every PR — score every tool call + argument + handoff, not just the final answer, since "agent failures often start before the final response" — with a scorer mix of 60% deterministic (exact match, regex, JSON-schema validation, latency threshold), 30% LLM-as-judge, 10% human, and "block-on-regression, not block-on-absolute-threshold." (3) Structured-JSON logging of every run (user input, all tool calls/responses, LLM outputs, latency, cost, session/tenant id) because "you cannot replay, debug, or mine what you didn't log."

### One SourcePort satisfied by both API and browser adapters [medium, non-load-bearing]
Declare a single `discover()`/`register()` port contract implemented once as an HTTP-connector adapter and once as a Claude-in-Chrome computer-use adapter — "migrating from one framework to another only requires changing the infrastructure layer." MCP-exposed tools "are indistinguishable from the built-in file and shell tools," so both adapter kinds surface uniformly to the orchestrator. Selection is a deterministic per-source capability flag (`has_api?`) plus health/fallback, not model reasoning, keeping the found→registered→scheduled state machine identical regardless of executing adapter.

### Orchestrator as Claude Agent SDK while-loop with isolated subagents [medium, non-load-bearing]
The SDK core is "a simple while-loop that calls the model, runs tools, and repeats," wrapped by a permission system (seven modes + ML classifier), a five-layer compaction pipeline, and subagent delegation where "each subagent has its own context window, prompt, and tool permissions" (messages carry `parent_tool_use_id`). Run a long, noisy browser registration in a dedicated subagent (isolated context, scoped credentials) that returns a normalized Registration to the pure domain layer; idempotency, saga state, and the lifecycle machine live in deterministic domain code the loop calls into, never in the prompt.

## Design implications

1. **Pure domain core, zero framework imports.** User, EventRequest, CandidateEvent, Registration, and the found→registered→scheduled state machine import no Claude SDK, browser, or HTTP libs. The LLM is a driven port (RankingPort/ReasoningPort), the browser is a driven port, calendar/payment/source connectors are driven ports. Non-determinism lives only in adapters.

2. **One SourcePort, two adapters, deterministic selection.** `discover(criteria)->CandidateEvent[]` and `register(user, event)->Registration`, implemented as `ApiSourceAdapter` (preferred) and `BrowserSourceAdapter` (fallback for API-less/JS-heavy/invite-only). Strategy keyed on per-source `has_api?` + adapter health, never the model. Keep the port/tool surface minimal — every exposed schema is billed per turn.

3. **schema.org/Event canonical model behind an ACL.** Force Luma/Meetup/Eventbrite/Partiful JSON through a translator onto `{name,startDate,endDate,location,offers,eventAttendanceMode,eventStatus,organizer,url}`. Prefer parsing sites' existing JSON-LD Event blocks. De-dupe on a canonical key (normalized title + startDate + venue/organizer).

4. **Transactional outbox + CDC.** Write Registration state + outbox row in one Postgres transaction; publish via Debezium CDC (not polling) for low latency. Every downstream handler idempotent via processed-message-ID tracking.

5. **Idempotency as the human-confirmation replacement.** Stable client-derived `Idempotency-Key` per (user_id, event_id, action), persisted with `{status, body, external_ref}` PLUS a `recovery_point` + row lock. Scope the key so legitimate repeat actions (re-RSVP, second ticket) do not collapse onto a stale reply. Retries within the window replay the saved result.

6. **Orchestrated saga, irreversible steps last.** Consider Temporal for durable execution. Order so charge, confirmation email, and calendar write run LAST. Maintain a compensation registry mapping each action to an idempotent, partial-state-safe compensation, with forward recovery (notify user / release calendar hold) when a purchase is non-refundable.

7. **Relational state + outbox as default; event-source only the audit stream.** Append an immutable business-fact action-log (RegistrationAttempted/Charged/Confirmed/Compensated) for full auditability of autonomous money-moving actions.

8. **OTel GenAI instrumentation.** `invoke_agent → chat / execute_tool` span tree; `gen_ai.request.model`, `gen_ai.usage.*_tokens`, `gen_ai.input.messages`/`output.messages` (NOT deprecated `gen_ai.prompt`/`completion`). Tag every span with `tenant_id`/`user_id` for multi-tenant isolation and per-user cost/spend-limit enforcement.

9. **Three-layer test harness.** (a) Per-adapter recorded-HTTP fixtures + contract tests failing CI on source schema change; (b) ~30-case golden-transcript trajectory eval gating every PR, 60/30/10 deterministic/LLM-judge/human, block-on-regression vs recorded baseline; (c) structured-JSON logging of every run for replay and offline eval.

10. **Browser registration in an isolated subagent.** Scoped credentials, own context window, returns a normalized Registration to the deterministic orchestrator. Idempotency, saga state, spend limits, and per-user authorization enforced in domain code — never delegated to the prompt.

## Sources

- [Hexagonal Agents: What If Your App's Business Logic Was an AI Agent?](https://anoliphantneverforgets.com/notes/2026-03-18-hexagonal-agents) — Primary: LLM/agent as reasoning-engine core, tools as capability adapters, semantic late binding, token cost of adapter surface. [confirmed]
- [Applying Hexagonal Architecture in AI Agent Development (M. Fernández García)](https://medium.com/@martia_es/applying-hexagonal-architecture-in-ai-agent-development-44199f6136d3) — Ports/adapters for LLM providers, vector DBs, memory; framework-decoupling. Note: places LLM in infrastructure layer — weak corroborator for the reasoning-engine framing.
- [schema.org/Event](https://schema.org/Event) — Canonical property/enumeration reference (startDate, offers, eventStatus, eventAttendanceMode). [confirmed]
- [Anti-Corruption Layer Pattern (oneuptime)](https://oneuptime.com/blog/post/2026-01-30-anti-corruption-layer-pattern/view) — ACL translator boundary preventing external schema leakage.
- [Google Event structured data docs](https://developers.google.com/search/docs/appearance/structured-data/event) — Real-world JSON-LD Event emission by event sites. [confirmed]
- [microservices.io — Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html) — Same-transaction write, relay (polling vs log tailing), at-least-once, idempotent consumers, ordering. [confirmed]
- [Conduktor — Outbox Pattern for Reliable Event Publishing](https://www.conduktor.io/glossary/outbox-pattern-for-reliable-event-publishing) — Debezium 2.5+ CDC as current outbox relay. [confirmed]
- [Stripe API — Idempotent requests](https://docs.stripe.com/api/idempotent_requests) — Idempotency-Key header, 24h window, stored status+body, Idempotent-Replayed:true. [confirmed]
- [Stripe blog — Designing robust APIs with idempotency](https://stripe.com/blog/idempotency) — Rationale + retry semantics for money-moving idempotency. [confirmed]
- [brandur.org — Implementing Stripe-like Idempotency Keys in Postgres](https://brandur.org/idempotency-keys) — Postgres scheme with recovery_point + row lock (richer than response cache). [confirmed]
- [Temporal — Mastering Saga Patterns](https://temporal.io/blog/mastering-saga-patterns-for-distributed-transactions-in-microservices) — Orchestration vs choreography; delay irreversible steps; durable execution.
- [microservices.io — Saga pattern](https://microservices.io/patterns/data/saga.html) — Compensation idempotency + partial-state requirements.
- [Robust Agent Compensation (RAC), Perera et al., ACM CAIS '26](https://arxiv.org/pdf/2605.03409) — Compensation registry + forward recovery for irreversible agent actions.
- [Event Sourcing vs Transactional Outbox (I. Said)](https://medium.com/@ichsan.said/using-event-sourcing-transactional-outbox-pattern-in-event-driven-architecture-pros-cons-56dada9a4301) — When to choose outbox vs event sourcing for the action log.
- [OpenTelemetry blog — GenAI Observability](https://opentelemetry.io/blog/2026/genai-observability/) — invoke_agent/chat/execute_tool span tree and gen_ai.* attributes.
- [OpenLLMetry issue #3515](https://github.com/traceloop/openllmetry/issues/3515) — Currency: v1.38.0 deprecates gen_ai.prompt/completion for input/output.messages.
- [Datadog — native OTel GenAI conventions](https://www.datadoghq.com/blog/llm-otel-semantic-convention/) — Vendor adoption.
- [FutureAGI — LLM Regression Testing Guide (2026)](https://futureagi.com/glossary/llm-regression-testing/) — Golden datasets, replay regression on PR, structured JSON logging.
- [Agent Evaluation: Tools, Trajectories, LLM-as-Judge (V. Rane)](https://medium.com/@vinodkrane/chapter-8-agent-evaluation-for-llms-how-to-test-tools-trajectories-and-llm-as-judge-788f6f3e0d52) — Trajectory scoring of tool calls/args/handoffs.
- [AI Agent Eval Frameworks 2026 (Digital Applied)](https://www.digitalapplied.com/blog/ai-agent-eval-frameworks-testing-guide-2026) — 60/30/10 scorer mix; block-on-regression.
- [Inside Claude Code's architecture](https://callsphere.ai/blog/inside-claude-code-s-architecture-how-the-agent-loop-works) — while-loop core, MCP tools indistinguishable from built-ins.
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) — Official SDK: loop, permissions, subagents, MCP.
- [Claude Code Agent Teams, Subagents, MCP: 2026 Playbook](https://www.developersdigest.tech/blog/claude-code-agent-teams-subagents-2026) — Subagent isolated context windows, scoped permissions, parent_tool_use_id.
