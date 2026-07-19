# Agent Orchestration for a Stateful, Money-Moving Events Concierge (2026)

> The concierge spends real money (paid RSVPs, ticket purchases) across many tenants and waits days for external confirmations — so where the lifecycle state lives and how side effects are made exactly-once determines whether the system is production-safe or silently double-charges users.

## Verified findings

### Claude Agent SDK "sessions" are a transcript store, not a workflow-durability layer [adjusted]

A Claude Agent SDK "session" is the accumulated conversation transcript — prompt + tool calls + tool results + responses — not a durable workflow-execution engine. There is no crash/exactly-once execution guarantee, so a session cannot serve as the money-safety layer. [confirmed]

Corrected details (the central thesis holds; three points needed correction, none of which change the design conclusion):

- By default the SDK writes the transcript as JSONL to local disk (`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, or `$CLAUDE_CONFIG_DIR`). This default is durable on that machine but local to it and keyed to `cwd`. [adjusted — the earlier claim that the default store is a non-production in-memory store was wrong; the docs call the local JSONL "already durable on disk."]
- Resume with `resume=<session_id>` or `continue:true` (most-recent-in-cwd); `fork_session=True` branches history. [confirmed]
- The SDK also ships an opt-in `InMemorySessionStore` documented as "for development and testing" (state lost on process exit) — but it is NOT the default. [adjusted]
- For multi-host / serverless / cross-host durability, implement the `SessionStore` protocol (append/load) backed by S3/Redis/Postgres (reference adapters exist) to mirror transcripts, OR move the JSONL to a matching `cwd` on the new host. Single-host production can rely on the durable local JSONL. [adjusted — "production requires a SessionStore" overstated; only cross-host does.]
- Anthropic's own robustness guidance: do not rely on session resume for cross-host continuity; instead capture the results you need as application state and re-seed a fresh session. Treat the conversation log as recoverable context, not the system of record for workflow state. [adjusted — the two verbatim "quotes" in the original claim were not found in the July 2026 docs and appear paraphrased/fabricated; the actual guidance is as stated here.] The `SessionStore`/`InMemorySessionStore` language lives on a third doc, `/agent-sdk/session-storage`, not on `sessions` or `subagents`.

### Checkpointing is not durable execution [confirmed]

LangGraph's `MemorySaver`/checkpointer (and by extension the Claude SDK transcript) snapshots state at node/superstep boundaries but does not detect failures, does not survive mid-node crashes, and offers no distributed exactly-once coordination. [confirmed by both primary sources]

- Framing (Diagrid, verbatim): checkpoint = "I saved your state. You take it from here" vs durable execution = "Your agent workflows will run to completion. Period. I handle everything."
- No automatic failure detection: "If your process crashes, no one knows" — "no supervisor, no watchdog, no heartbeat mechanism." [confirmed]
- No automatic resumption — manual `invoke(None, config)` with the correct `thread_id`. [confirmed]
- No duplicate-execution prevention (verbatim): two processes resuming the same `thread_id` simultaneously have "no built-in coordination to prevent both from executing" → shared-state corruption. [confirmed]
- LangGraph `interrupt()` gives a durable pause/resume for human approval ONLY if compiled with a persistent checkpointer + a stable `thread_id`. On resume "the runtime restarts the entire node from the beginning… any code that ran before the `interrupt()` will execute again." The docs instruct "Place side effects after `interrupt()` calls" and warn against non-idempotent operations before it (they "create duplicate records on each resume"). [confirmed]
- Net for a money-spending agent: transcript/checkpoint resumption alone can fire a non-idempotent side effect (e.g. a payment) twice. Correct structuring (idempotent ops, side effect after the interrupt boundary) mitigates it — but the framework gives no guarantee. [confirmed]

### A durable-execution engine supplies the primitives the register step needs [confirmed]

Temporal (the reference engine) provides `workflowId`-as-idempotency-key, zero-compute multi-day waits, signals for external events, and saga compensation — with LLM/tool calls wrapped as retryable Activities. [confirmed]

- Pattern: agent loop + decision logic run in the deterministic Workflow; every non-deterministic action (LLM call, browser registration, payment, calendar write) is an Activity with a retry policy (`retry:{maximumAttempts:3}`, `startToCloseTimeout`). Completed Activity results are cached in append-only event history; after a worker crash a new worker replays history and resumes where execution stalled, never re-running completed Activities. [confirmed]
- `workflowId` as idempotency key: a duplicate `StartWorkflow` with the same ID never yields two active runs per ID. Correction to note in a design doc: "returns the existing execution instead of starting a duplicate" is only literal with `WorkflowIdConflictPolicy=USE_EXISTING`; the DEFAULT policy is `Fail` (rejects with `WorkflowExecutionAlreadyStartedFailure`). Either way a duplicate is prevented, but returning the running handle requires opting into `USE_EXISTING`. [adjusted]
- Long waits (registration needing email confirmation) use `workflow.wait_condition()`/`await` with a timeout of days — the worker returns the task and goes idle (no compute) until a Signal arrives or the timer fires. [confirmed]
- Exactly-once external events: Update IDs are server-deduplicated. Correction: Signals are NOT auto-deduplicated — the docs say for Signals "you should use a custom idempotency key… implementing the deduplication in your Workflow code." So a Signal-delivered payment trigger needs its own idempotency key. [adjusted]
- Vercel AI SDK integration is near-drop-in: `temporalProvider.languageModel('...')` instead of `openai('...')`; "the plugin automatically wraps the LLM call in a Temporal Activity" (via `AiSDKPlugin`), supporting OpenAI/Anthropic/Google. [confirmed verbatim]
- Replay 2026: Nexus (workflow-to-workflow) is GA for the Python SDK; the OpenAI Agents SDK integration is GA with added sandbox isolation. [confirmed]

### Claude Agent SDK subagents give least-privilege decomposition, resumable via agentId, but durability is transcript-based [confirmed]

Subagents give context isolation and specialized tool-scoping (a strong fit for per-source register adapters) and CAN be resumed via `agentId`, but their durability is transcript-based, not execution-durable. [confirmed]

- Context isolation: "Each subagent runs in its own fresh conversation. Intermediate tool calls and results stay inside the subagent; only its final message returns to the parent." The only parent→child channel is the Agent tool's prompt string. [confirmed]
- Tool-scoping = real least privilege: `AgentDefinition` fields `description`, `prompt`, `tools`, `disallowedTools`, `model` override (`opus`/`sonnet`/`haiku`, also `fable`/`inherit`/full IDs), `maxTurns`, `background` (non-blocking), `permissionMode`. A discovery subagent can be scoped to Read/Grep/Glob; a register subagent to only that source's tools + the payment tool. [confirmed]
- Resumption: the Agent tool result includes `agentId: <id>`; capture `session_id` + `agentId`, then `resume=session_id` with the `agentId` named in the prompt to continue the subagent's full transcript ("picks up exactly where it stopped"). Built-in Explore/Plan agents are one-shot and return no `agentId`. [confirmed]
- Nesting up to 5 levels (v2.1.172); a subagent five levels below cannot spawn further. Transcripts persist in separate files (survive main-conversation compaction), cleaned up per `cleanupPeriodDays` (default 30). [confirmed]
- This gives least-privilege decomposition but not by itself exactly-once money semantics. [confirmed inference]

### The 2025→2026 shift: orchestration moved out of the model's context [medium confidence, non-load-bearing]

The winning pattern is a thin native tool-use loop + MCP for tools, with durable control flow codified in external code rather than carried in messages. Claude Code "dynamic workflows" (research preview, week of May 25–29 2026, alongside Opus 4.8, requires v2.1.154+) make this explicit — "the plan moved out of the conversation and into a file": a JS script whose loop/branching/intermediate results live in script variables, run by a runtime in the background while the session stays free (Agent SDK Workflow tool, TS SDK v0.3.149+). Industry-wide: OpenAI Agents SDK's minimal "model + tools + loop" displaced heavy 2024 abstractions; MCP became the universal tool protocol; LangGraph hit v1.0 (Oct 2025); Microsoft Agent Framework unified AutoGen + Semantic Kernel (Oct 2025). Emerging consensus: a durable engine owns macro lifecycle/retries/waits; the agent framework owns micro reasoning inside each step — but wrapping a whole agent run as one opaque Activity loses fine-grained replay/visibility.

### Structural limits of LangGraph vs Temporal [medium confidence, non-load-bearing]

- LangGraph: HITL is a single `interrupt()`; short/long-term memory built in; "routinely handles payloads in the hundreds of megabytes" inside state. But the checkpointer "only saves state between nodes, not inside a node," and there is no built-in distributed lock or saga/compensation.
- Temporal: "no concept of prompts, context windows, or LLM state" — you manually track message history/summaries/retrieval; workflow code must be deterministic (LLM nondeterminism pushed into Activities; needs workarounds like Worker Versioning); its gRPC event-history model imposes a ~2MB payload cap, forcing large blobs (screenshots, DOM, event lists) into external object storage referenced by key.
- Practical read: keep bulky discovery/ranking payloads out of the durable workflow's arguments (pass S3/DB keys); do not rely on LangGraph checkpoints as the money-safety layer.

## Design implications

1. **State machine lives in an external durable store, not in agent context.** Model one durable workflow instance per `(tenant_user, event)` request with lifecycle `found → registered → scheduled_to_calendar`. The Claude Agent SDK session/transcript is a disposable reasoning cache; the durable event history (or a Postgres lifecycle table) is the source of truth.

2. **Make the REGISTER step a durable-execution saga** (Temporal, or Restate/DBOS/Inngest). Wrap each irreversible sub-step (submit registration, charge ticket, confirm) as an idempotent Activity with a retry policy; add compensation activities (cancel/refund where possible) so a partial failure after payment is recoverable, not a silent double-charge.

3. **`workflowId = {user_id}:{event_id}` is the idempotency key.** A retried API call or duplicate discovery hit maps to the SAME running registration. Set `WorkflowIdConflictPolicy=USE_EXISTING` explicitly (the default is `Fail`) so start is idempotent. Never rely on Claude SDK session resume or LangGraph checkpoints for exactly-once on payments — those replay side effects.

4. **Model "registration needs email confirmation" as a durable timer + signal**, not a blocking agent turn. Use `wait_condition` with a multi-day timeout; an inbox/webhook poller sends a Signal that resumes the workflow; on timeout the workflow escalates or marks the event failed. The idle wait must consume no compute and survive restarts. Because Signals are not server-deduplicated, attach a custom idempotency key to any Signal that triggers payment.

5. **Decompose via Ports-and-Adapters using subagents for least privilege, not durability.** A discovery subagent scoped to read/search + connector MCP tools; a per-source register subagent (API adapter vs Claude-in-Chrome browser adapter, each satisfying one Register port) scoped to only that source's tools + the payment tool; a calendar subagent scoped to calendar MCP. Pass everything a subagent needs in its Agent-tool prompt (fresh context) and keep bulky page/DOM/screenshot payloads in object storage referenced by key (respect the ~2MB durable-payload cap).

6. **Adopt the macro/micro split.** The durable engine owns lifecycle, retries, waits, saga; the Claude native tool-use loop (Opus for planning/register decisions, Sonnet/Haiku for cheap discovery+ranking) owns reasoning inside each step. Wrap each agent turn as a discrete Activity (not the whole run as one opaque Activity) to preserve replay/visibility, and drive tools via MCP so API and browser adapters share one interface.

7. **Treat the browser-driven register path as non-idempotent and slow.** Run Claude-in-Chrome/computer-use inside an Activity with heartbeating and a short attempt cap; persist a "submitted" marker before the network call; gate payment behind an explicit idempotency check so a mid-action crash + replay cannot re-submit a paid RSVP.

8. **Auditability and tenant isolation via event history + hooks.** Rely on the durable engine's append-only event history plus `PreToolUse`/`PostToolUse` hooks: `PreToolUse` enforces per-user spending limits/authorization BEFORE a paid tool fires (it can block); `PostToolUse` writes the immutable audit record (it cannot undo). Store third-party credentials outside the transcript and inject them only into the scoped register subagent/Activity.

## Sources

- [Work with sessions — Claude Agent SDK docs](https://code.claude.com/docs/en/agent-sdk/sessions) — session = transcript JSONL at `~/.claude/projects/<encoded-cwd>`; continue/resume/fork; local JSONL is the durable default.
- [Session storage — Claude Agent SDK docs](https://code.claude.com/docs/en/agent-sdk/session-storage) — `InMemorySessionStore` is opt-in "for development and testing"; `SessionStore` protocol for cross-host durability; re-seed-a-fresh-session guidance. (Not cited in original research; located during fact-check.)
- [Subagents in the SDK — Claude Agent SDK docs](https://code.claude.com/docs/en/agent-sdk/subagents) — `AgentDefinition` fields, context isolation, tool restriction, `agentId` resumption, nesting depth (v2.1.172), transcript persistence/`cleanupPeriodDays`.
- [Checkpoints Are Not Durable Execution — Diagrid](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows) — checkpointing (LangGraph/CrewAI/ADK) misses failure detection, mid-node crashes, distributed exactly-once.
- [Building durable agents with Temporal and AI SDK by Vercel — Temporal](https://temporal.io/blog/building-durable-agents-with-temporal-and-ai-sdk-by-vercel) — workflow vs activity split; `temporalProvider.languageModel` auto-wraps LLM calls; retry/idempotency; replay resume.
- [Human-in-the-Loop Approval Workflows — Temporal](https://temporal.io/blog/human-in-the-loop-approvals) — `wait_condition` multi-day waits, idle workers, signal-based resume, timer/SLA escalation, idempotent signal handlers.
- [Handling Signals, Queries & Updates — Temporal docs](https://docs.temporal.io/handling-messages) — Update-ID server-side dedup (exactly-once); Signals require custom idempotency keys.
- [WorkflowId conflict policy — Temporal docs](https://docs.temporal.io/workflow-execution/workflowid-runid) — default `Fail` vs `USE_EXISTING`; start-idempotency semantics.
- [Announcing new Temporal capabilities from Replay 2026 — Temporal](https://temporal.io/blog/replay-2026-product-announcements) — Nexus GA (Python), OpenAI Agents SDK GA integration + sandbox, Serverless Workers.
- [LangGraph vs Temporal — LangChain](https://www.langchain.com/resources/langgraph-vs-temporal) — native state/memory + `interrupt()` in LangGraph; Temporal determinism requirement + 2MB payload cap; run-both pattern.
- [Interrupts — LangChain/LangGraph docs](https://docs.langchain.com/oss/python/langgraph/interrupts) — `interrupt()` needs durable checkpointer + `thread_id`; pre-interrupt code replays on resume; side-effect placement rules.
- [Orchestrate subagents at scale with dynamic workflows — Claude Code docs](https://code.claude.com/docs/en/workflows) — orchestration codified as a JS script run outside the conversation; Agent SDK Workflow tool.
- [A harness for every task: dynamic workflows — Anthropic](https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code) — "the plan moved out of the conversation into a file"; Opus 4.8, v2.1.154+, May 2026.
- [The AI Agents Stack (2026 Edition) — O'Reilly Radar](https://www.oreilly.com/radar/the-ai-agents-stack-2026-edition/) — 2025→2026 shift to thin loops over provider APIs + MCP; framework consolidation.
