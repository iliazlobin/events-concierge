# Durable-Execution Engine Selection and the Saga / Idempotency Spine

> Closes the post-wave-1 gap where the found→registered→scheduled lifecycle lived inside the agent framework: checkpoint-style resume re-executes side effects (double-RSVP), so the lifecycle must move onto a true durable-execution engine with an explicit idempotency spine and a separate browser-path double-submit guard.

## Verified findings

### 1. Wave 1's premise holds: checkpoint/replay resume re-executes nodes and their side effects [confirmed]

On resume with the same `thread_id`, LangGraph does not continue from the next line of source — it replays from the start of the node where it stopped. Every side effect in that node (LLM calls, API/HTTP requests, file writes, interrupts) is repeated unless explicitly managed; LangGraph's own docs make determinism and idempotency the developer's responsibility. There is no built-in mechanism to prevent side-effect re-execution for plain `StateGraph` nodes, and no built-in coordination to stop two processes resuming the same `thread_id` from both executing. Contrast with true durable execution (Temporal/Restate/DBOS), where completed activities return their recorded results from the event log and only incomplete steps re-execute. This is the exact mechanism that can fire an already-performed RSVP twice.

- Minor nuance (does not change the conclusion): LangGraph's Functional API `@task` decorator *does* retrieve recorded results instead of re-running, but it is opt-in per side effect, does not cover plain `StateGraph` nodes, and does nothing for concurrent same-`thread_id` resumption. Idempotency remains the developer's problem.

### 2. Temporal payload limits are real; claim-check / External Storage is mandatory [confirmed]

Temporal's per-payload limit is 2 MB (hard on Temporal Cloud; configurable on self-hosted with a 2 MB default), and gRPC enforces 4 MB per request including all command metadata. Because a Workflow Task returns all scheduled commands and their inputs in one gRPC frame, several medium payloads can jointly breach 4 MB and fail the whole task. Fix: claim-check pattern, built into the SDKs as External Storage or via a custom Payload Codec — pass references to stored payloads through the workflow, retrieve from object store when needed, and apply it proactively even when currently within the limit. Comparison points: Inngest caps step output at 4 MiB, total function-run I/O at 32 MiB, event payloads plan-gated (256 KiB Free → 3 MiB Pro); DBOS/Restate persist inputs/outputs as Postgres/journal rows and likewise recommend S3-pointer offload.

### 3. The email-confirmation wait is a first-class durable primitive in all four engines [confirmed]

"Registration needs email confirmation" is a durable wait (timer + external signal) that consumes zero worker resources and survives crashes — replacing the wave-1 blocking-agent-turn anti-pattern. The differentiator is the ops model, not capability.

| Engine | Durable timer | External-signal wait | Zero-resource while waiting |
|---|---|---|---|
| Temporal | `workflow.sleep` (arbitrarily long) | `workflow.wait_condition` + Signal | Yes — persisted record only, survives worker crash/replay |
| Restate | `ctx.sleep()` | `ctx.awakeable()` durable promise resolved by webhook/callback | Yes — handler suspends, re-invoked on result |
| Inngest | `step.sleep` / `step.sleepUntil` (up to ~1 year) | `step.waitForEvent()` (event or `null` on timeout) | Yes — explicitly does not count toward capacity/concurrency limits |
| DBOS | `sleep` (wake-up time recorded in Postgres) | `send`/`recv` (exactly-once), `setEvent`/`getEvent`; documented human-approval `recv` | Yes — resumes toward stored wake-up time across restarts |

- Caveats surfaced during fact-check: the cited Temporal AI-cookbook URL 404s (substance verified via temporal.io); Restate awakeable specifics live on the `develop/` pages, not the generic key-concepts page; Inngest max-sleep and the DBOS human-approval example were confirmed from adjacent official pages. None undermine the claim.

### 4. Activities are at-least-once; exactly-once effect needs an idempotent activity + a key minted once in workflow state [confirmed]

Temporal activities may execute more than once (worker can succeed then crash before acknowledging); the platform does not enforce idempotence. Exactly-once business effect comes only from an idempotent activity plus a duplicate-rejecting Workflow ID. The canonical key is `workflowRunId + "-" + activityId` — constant across retry attempts, unique among Workflow Executions. Documented failure mode: generating a UUID *inside* the activity yields a new value per retry (activities re-run from scratch with no in-execution memoization), so a downstream gateway sees a "new request." Mint the key in deterministic workflow code and pass it in. DBOS gives transactional exactly-once only for `@DBOS.Transaction` steps writing the same Postgres that stores workflow state; any external-API step is back to at-least-once plus an idempotency key. Restate deduplicates on a client-supplied `Idempotency-Key` header and replays recorded `ctx.run()` results. Maps 1:1 to our "one workflow per (user,event), workflowId as idempotency key" decision.

- Pedantic refinement: the Temporal canonical key is deterministically *derived* rather than literally stored in workflow state, but the operative rule — generated in workflow code, never inside the retried activity — is exactly right.

### 5. Engine idempotency does NOT protect the site's own state on the browser path [confirmed / high]

The browser RSVP POST to Luma/Meetup/Partiful carries no merchant-honored idempotency key, so a retry after a lost ACK can create a second RSVP even though the engine invoked the activity effectively once from our side. Industry 2026 practice for browser/tool agents: query order/registration history before retrying, and guard with a state check before proceeding. Concretely, split the browser RSVP activity into (a) a read-only detect step (scrape the "You're going" / registration-confirmed marker or the user's RSVP/order history) and (b) a submit step guarded by that detect. On retry, re-run detect first: CONFIRMED → idempotent no-op success; NOT_PRESENT → safe to submit; AMBIGUOUS (timeout, layout change, login wall, best-effort browser source off the SLA path) → emit a durable signal/awakeable and hand off to human review rather than re-POST.

### 6. Operational footprint split (non-load-bearing context) [high confidence]

DBOS embeds durable execution as a library over your existing Postgres — one DB, one deploy, one metrics set (integration cited at ~7 lines in a 110-LoC app), self-host only, breaks first on Postgres contention (every step ≥1 write), ceiling ~a few thousand state-transitions/sec. Temporal requires splitting the app into two services (>100 LoC changed); self-host is three services + persistence + metrics + worker fleet ("not a weekend project for a three-person team"), scales to tens of thousands of transitions/sec, with Temporal Cloud removing ops overhead at a per-state-transition price. Restate is a lightweight sidecar (simpler ops, younger). Inngest/Trigger/Hatchet are shaped for durable background jobs / event-driven work, not long-lived per-entity stateful sagas. All four ship first-class Python and TypeScript SDKs.

### 7. Recommendation: Temporal as the spine; DBOS as the deliberate lighter-weight fallback [adjusted — confidence: medium]

Temporal is the best fit for a multi-tenant product with reliability SLAs and long-lived, per-entity, signal-driven workflows: mature at scale, arbitrarily long durable timers + signals for the confirmation wait, `workflowId`-as-idempotency-key matching our "one workflow per (user,event)" design, at-least-once activities + idempotent adapters giving effective exactly-once, and Temporal Cloud to dodge the 3-service self-host burden at launch. Main tax: the 2 MB payload limit → adopt claim-check/External Storage from day one. DBOS is the honest runner-up because we already run Postgres (pgvector for ranking): near-zero added footprint and transactional exactly-once for DB steps — but the few-thousand-transitions/sec ceiling, coupling of workflow durability to the application Postgres, and thinner large-scale-multitenant track record push it second. Restate (younger) and Inngest (background-job/event shaped, plan-gated payloads) are weaker for this long-running stateful saga.

## Design implications

Directives for our system:

1. **Own the lifecycle in Temporal, not LangGraph.** Remove found→registered→scheduled from the Claude/LangGraph tool-use loop. The agent's native tool-use loop runs *inside* Temporal Activities for reasoning only; every side-effecting call (SourcePort discovery, RSVP POST, calendar write) is its own Activity so it is journaled and never replayed. This is the concrete fix for the wave-1 side-effect-replay failure.

2. **One Workflow per (user_id, event_id).** Set `workflowId = f"{user_id}:{canonical_event_id}"` with a reject-duplicate `WorkflowIdReusePolicy`, so a re-submitted request for the same (user,event) is deduped by the engine rather than ad-hoc app logic.

3. **Saga shape:** (1) `discover_candidates` → (2) `rank` → (3) `register_or_rsvp` → (4) `await_email_confirmation` (durable wait) → (5) `dedupe_calendar` → (6) `write_to_calendar`. Persist lifecycle transitions (found / registered / scheduled_to_calendar) as workflow state, not the app DB, so status is crash-consistent.

4. **Mint idempotency keys once in workflow state, never inside a retried activity.** For API-clean SourcePort adapters that honor keys, use `workflowRunId + "-" + activityId`. The calendar write must be idempotent via a deterministic key (e.g. hash of `user_id` + canonical event id) plus a de-dup pre-read (matches the existing calendar de-duplication requirement).

5. **Mandate claim-check / External Storage from day one.** Screenshots, raw DOM, and candidate-event lists go to S3/object storage; only the object KEY travels through Temporal payloads. Enforce a hard cap well under 2 MB on any inline activity input/output; add a Payload Codec that auto-offloads oversize blobs.

6. **Confirmation wait = durable wait, not a blocking turn.** Implement step (4) as `workflow.wait_condition` on a Signal (or an awakeable-style callback) fed by the confirmation-email watcher, with a durable timeout (e.g. `sleep` 24h) that routes to human-handoff on expiry. Zero worker resources held during the wait.

7. **Browser-path anti-double-RSVP is a separate guard from engine idempotency.** Split the browser RSVP activity into a read-only detect step and a guarded submit step. On every retry, re-run detect first: CONFIRMED → idempotent no-op success; NOT_PRESENT → submit; AMBIGUOUS → durable signal to human review, never blind re-POST. Keeps browser-only sources (Partiful, Eventbrite discovery, Luma registration) off the reliability-SLA critical path.

8. **Launch on Temporal Cloud; keep the exit open.** Avoid the 3-service self-host burden while retaining SLA-grade scale. Keep the Temporal server behind a port so a move to self-hosted (or a fallback to DBOS on the existing Postgres) is an infra swap, not a code rewrite. Use the Python SDK to match the Claude tool-use activities; the TS SDK remains available if browser adapters are written in TypeScript.

9. **Treat DBOS as a documented fallback.** If operational appetite for a Temporal cluster/cloud spend is low, DBOS over the existing pgvector Postgres gives transactional exactly-once with minimal footprint — accept the few-thousand-transitions/sec ceiling and plan identical S3 blob offload (steps return pointers, not files).

## Sources

- [Troubleshoot payload / gRPC message size limit errors — Temporal Docs](https://docs.temporal.io/troubleshooting/blob-size-limit-error) — PRIMARY: 2 MB payload / 4 MB gRPC hard limits; claim-check / External Storage; default configurable on self-host.
- [System limits — Temporal Cloud Docs](https://docs.temporal.io/cloud/limits) — Confirms 2 MB payload limit applies on Temporal Cloud; managed-service constraints.
- [Durable execution — LangChain/LangGraph Docs](https://docs.langchain.com/oss/python/langgraph/durable-execution) — PRIMARY: resume re-runs nodes and re-triggers side effects/interrupts; developer must make side-effect nodes idempotent.
- [Checkpoints Are Not Durable Execution — Diagrid](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows) — No built-in dup-execution prevention/coordination on same `thread_id` resume; contrasts replay-from-event-log durable execution.
- [Activity Definition — Temporal Docs](https://docs.temporal.io/activity-definition) — PRIMARY: activities are at-least-once; platform does not enforce idempotence.
- [What is idempotency? — Temporal Blog](https://temporal.io/blog/idempotency-and-durable-execution) — `workflowRunId + "-" + activityId` key; UUID-inside-activity pitfall; wrap-and-verify for exactly-once effect.
- [Human-in-the-Loop AI Agent — Temporal Docs](https://docs.temporal.io/ai-cookbook/human-in-the-loop-python) — Durable timers + Signals for confirmation/approval waits (cited URL 404s; substance verified via temporal.io).
- [Key Concepts — Restate Docs](https://docs.restate.dev/foundations/key-concepts) + [Awakeables (TS)](https://docs.restate.dev/develop/ts/awakeables/) / [Awakeables (Go)](https://docs.restate.dev/develop/go/awakeables/) — `ctx.run` durable steps, header idempotency-key auto-dedup, `ctx.sleep`, `ctx.awakeable` callback waits; Python + TS SDKs.
- [Inngest Usage Limits](https://www.inngest.com/docs/usage-limits/inngest) / [wait-for-event](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/wait-for-event) / [concurrency](https://www.inngest.com/docs/guides/concurrency) — Step output 4 MiB, 32 MiB/run, plan-gated event payloads; `waitForEvent`/`sleep` do not count toward capacity/concurrency.
- [DBOS Transact (Python) — GitHub](https://github.com/dbos-inc/dbos-transact-py) + [Workflow Tutorial](https://docs.dbos.dev/python/tutorials/workflow-tutorial) — Library over Postgres; `@DBOS.Transaction` exactly-once DB steps; workflow idempotency key = workflow ID; durable `sleep`; recommends S3 pointers for large outputs.
- [Communicating with Workflows — DBOS Docs](https://docs.dbos.dev/golang/tutorials/workflow-communication) — `send`/`recv` exactly-once messaging, `setEvent`/`getEvent`; human-approval `recv` pattern.
- [DBOS vs Temporal: Choosing Durable Execution in 2026 — Tiarebalbi](https://tiarebalbi.com/en/blog/dbos-vs-temporal-postgres-durable-execution) — Footprint (1 vs 3 services), self-host vs Temporal Cloud pricing, scale ceilings, idempotency semantics (external-API steps back to at-least-once).
- [Durable Execution: Temporal, Restate, DBOS (2026)](https://devstarsj.github.io/2026/04/03/durable-execution-temporal-restate-dbos-distributed-workflows-2026/) — Architecture split: Temporal cluster vs Restate sidecar vs DBOS library; operational trade-offs.
- [Idempotent AI Agents: Retry-Safe Patterns for Production (2026)](https://www.buildmvpfast.com/blog/idempotent-ai-agent-retry-safe-patterns-production-workflow-2026) — Browser/tool agent double-submit during retry windows; query order history before retry; dedup table + deterministic keys.
- [How to Build Idempotent Tool Calls for AI Agents — Channel](https://www.channel.tel/blog/idempotent-tool-calls-agent-retry-safety) — Idempotency-guard: check state before proceeding; keys deterministic from workflow context, not execution moment.
- [Temporal alternatives — ZenML](https://www.zenml.io/blog/temporal-alternatives) — Positioning of Inngest/Trigger/Hatchet as background-job/event-driven vs long-lived per-entity sagas.
