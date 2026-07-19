# Cost, Efficiency & Scale Economics for a Multi-Tenant Autonomous Events Concierge

> Per-registration unit cost is dominated by LLM tokens, not browser infrastructure; model tiering, prompt caching, cross-tenant discovery reuse, and API-vs-browser adapter mix are the levers that decide whether the system is profitable at scale.

## Verified findings

### The pricing ladder and model tiering (load-bearing)

[adjusted] The 2026 Claude API price ladder gives roughly a **5-10x like-for-like spread** across the tier — not the "5-25x" originally claimed. Verified verbatim against the official pricing page (per MTok, input/output):

| Model | Input | Output |
|---|---|---|
| Haiku 4.5 | $1 | $5 |
| Sonnet 4.6 | $3 | $15 |
| Sonnet 5 (intro thru Aug 31 2026) | $2 | $10 |
| Sonnet 5 (from Sep 1 2026) | $3 | $15 |
| Opus 4.8 | $5 | $25 |
| Fable 5 | $10 | $50 |

Haiku -> Opus is 5x; Haiku -> Fable is 10x. Output is exactly **5x input on every tier**, so verbose reasoning is the expensive part. A single hard registration on Opus costs ~5x the same flow on Haiku for identical tokens. This confirms the core thesis: cheap model for routing/extraction, expensive model only for hard registration reasoning. The "25x" figure only appears if you cross input-vs-output tiers (Haiku input $1 vs Fable output $50), which is not a like-for-like comparison. Two caveats: (1) Fable 5 availability may be restricted per a June 12 2026 US government directive — price still listed, access constrained; (2) tokenizer inflation applies to newer models (see below).

[confirmed] **Newer tokenizer inflates effective cost.** Opus 4.7+, Sonnet 5, Fable 5, and Mythos 5 use a newer tokenizer that produces **~30% more tokens for the same text**; Sonnet 4.6 and earlier use the previous tokenizer. Effective cost of Opus 4.8 vs Sonnet 4.6 on identical English text is therefore ~(5x1.30)/3 ≈ **2.2x** on input and output — not the ~1.67x the sticker implies. This nudges the default reasoning model toward Sonnet 4.6.

### Prompt caching (load-bearing)

[confirmed] **Prompt caching cuts repeated context to 10% of input price** (cache read = 0.1x base), writes at 1.25x (5-min) or 2x (1-hour). Break-even after 1 read (5-min: 1.25 + 0.1 = 1.35x < 2x) or ~2 reads (1-hour: 2 + 0.2 = 2.2x < 3x). Concrete cache-read prices: Sonnet $0.30/MTok vs $3 base; Opus $0.50 vs $5; Haiku $0.10 vs $1.

[confirmed] **Cache-read tokens do NOT count against ITPM** (the per-minute input token rate limit). The rate-limits doc has an explicit "Cache-aware ITPM" section: `input_tokens` count, `cache_creation_input_tokens` (writes) count, `cache_read_input_tokens` do **not**. Worked example from the doc: "With a 2,000,000 ITPM limit and an 80% cache hit rate, you could effectively process 10,000,000 total input tokens per minute" — i.e. **5x throughput at 80% hit**, higher at higher hit rates. A large shared system prompt + tool schemas + policy can be cached once and reused across all tenants without consuming rate-limit budget on the read leg.

[confirmed] **Workspace-level cache isolation as of Feb 5 2026** (was org-level) on the Claude API, AWS, and Microsoft Foundry; Bedrock and Google Cloud keep org-level isolation — relevant to tenant isolation.

Two caveats carried into design: the ITPM exclusion is "most models" — Claude Haiku 3.5 (retired on first-party API) is the one exception that counts cache reads; and cache-WRITE tokens still count toward ITPM, so the shared prefix consumes budget on (re)write, not on reuse.

### Per-registration token economics (load-bearing)

[adjusted] **The browser/computer-use registration loop is dominated by LLM tokens, not browser infrastructure.** Verified token math (base rates, 0.1x cache-read multiplier):

- 25-step task, ~400K cumulative input @ 75% cache hit, 10K output: **$0.54 (Sonnet 4.6), $0.90 (Opus 4.8), $1.80 (Fable 5)** — figures reconcile exactly.
- 100-step task, 2M cumulative input, 40K output: **$2.28 / $3.80 / $7.60** — but these assume **~80% cache hit, not 75%**. At 75% they would be ~$2.55 / $4.25 / $8.50.

Cumulative context grows because each step re-sends history (screenshots + DOM). Figures treat the non-cached fraction at full input price, ignoring the 1.25x first-write premium, so they are mild underestimates. A browser registration is structurally identical to these agentic runs. Token cost is **1-2 orders of magnitude above browser/session compute** (managed-agent runtime $0.08/session-hour; code-execution containers $0.05/hour), confirming tokens as the load-bearing per-registration cost driver. (The originally-cited morphllm.com marketing page is unnecessary — the math is fully verifiable from Anthropic's own pricing.)

### Browser infrastructure is cheap (load-bearing)

[confirmed] **Cloud headless-browser infra is a rounding error vs tokens.** Browserbase (verified against its own pricing page): Developer $20/mo (100 browser-hrs, $0.12/hr overage, 25 concurrent, 1GB proxy then $12/GB); Startup $99/mo (500 hrs, $0.10/hr, 100 concurrent, 5GB proxy then $10/GB). A 3-5 min session = 0.05-0.083 browser-hr x $0.10-0.12/hr = **~$0.005-0.010** — sub-cent, under 1-2% of the LLM cost of the same run. Self-hosting headless Chrome (a few hundred MB RAM per session) lands in the same range. **Concurrency caps (25-100 sessions) are real hard limits** that matter for tenant fan-out and must be provisioned/queued. Caveat: proxy bandwidth is separately metered ($8-12/GB residential; ~$0.30/GB datacenter tier) — a registration uses tens of MB (well under a cent), but heavy image/asset loading could push proxy cost above the raw browser-hour cost.

### Screenshots are the hidden token sink

[vision, high confidence] Each 28x28px patch = 1 visual token: 1280x800 ≈ **1,334 tokens**, 1920x1080 ≈ **2,691 tokens** on the high-res tier (Opus 4.8 / Sonnet 5, up to 2576px long edge / 4784 max visual tokens, full pixel charge). Standard tier (Sonnet 4.6, Haiku 4.5) caps at 1568px/1568 tokens and downscales larger images (1920x1080 -> ~1,560 tokens). Computer-use adds **466-499 token system-prompt overhead + 735 tokens per tool definition** (Claude 4.x). Because history re-sends every prior screenshot each turn, a naive 25-step run carries 25 accumulating images; pruning to the last 1-3 screenshots and/or using standard-tier downscaling is a major cost cut.

### Discovery is cheap and cacheable across tenants

[high confidence] `web_fetch` is free (pay only for fetched tokens; avg 10KB page ~2,500 tokens); `web_search` is **$10 per 1,000 searches** + token cost; code execution free when paired with search/fetch. Extracting structured event data from a 25K-token page on Haiku 4.5: ~$0.03 uncached, ~$0.003 on cache-read. Because event pages are tenant-agnostic, a shared discovery/extraction cache keyed by (source, event_id) with TTL to event start amortizes one fetch+parse over N interested users, collapsing per-user discovery cost toward zero. Per-source API/feed adapters (Eventbrite/Meetup) are cheaper still — plain JSON, no LLM extraction.

### Shared third-party quotas are the scaling ceiling (load-bearing)

[high confidence] **Eventbrite: ~2,000 API calls/hour (48,000/day) per token.** Meetup GraphQL and Luma/Partiful (no public API -> browser path) impose their own throttles and anti-bot friction. **Anthropic limits are per-organization, not per-key**: Tier 1 = 50 RPM / 30K ITPM up to Tier 4 = 4,000 RPM / 2M ITPM, token-bucket refill, cached tokens excluded from ITPM. At scale a single org token becomes the bottleneck. The design needs per-tenant concurrency caps + a global scheduler/queue, per-source rate-limit budgeting, and ideally **stored per-user third-party credentials** so quota draws against the user's own Eventbrite/Meetup account rather than one shared app token.

### Batch API (non-load-bearing)

[high confidence] **Flat 50% discount on input AND output**, stacks with caching (combined up to ~95% off). Batch rates: Haiku $0.50/$2.50, Sonnet 4.6 $1.50/$7.50, Opus 4.8 $2.50/$12.50. **Asynchronous — not available with Fast mode, and Managed Agents (stateful/interactive) sessions do not get the batch discount.** Route discovery + ranking (tolerant of minutes of latency) through Batch; keep the time-critical registration loop synchronous.

### Managed Agents runtime fee (non-load-bearing)

[high confidence] If Claude Managed Agents runs the browser sessions, add **$0.08 per session-hour** on top of tokens (metered only while status is 'running'; replaces code-execution container-hour billing). For a 3-5 min registration that is ~$0.004-0.007 — comparable to Browserbase. The choice between Managed Agents vs self-hosted Agent SDK + Browserbase is driven by isolation/ops concerns, not cost.

### Blended unit economics (non-load-bearing)

[medium confidence] **API-path registration costs < $0.05** (Haiku routing/extraction, few K tokens); **browser-path costs ~$0.50-0.90 (Sonnet) to ~$1-3 (Opus)** all-in — a **20-60x gap**. Failed browser attempts still burn tokens, so blended cost depends heavily on API-vs-browser mix and browser success rate. At a modest ~$2-5/month subscription or ~$0.50-1 per-successful-registration fee, a mostly-API mix is comfortably profitable; a browser-heavy, Opus-default, low-success-rate config can exceed $2-3 per confirmed event and erode margin.

## Design implications

1. **Strict 3-tier model routing as a first-class component.** Haiku 4.5 for intent parsing, event extraction, dedup, field-mapping; **Sonnet 4.6 as the default registration-reasoning model** (old tokenizer makes it ~2.2x cheaper in effective tokens than Opus); escalate to Opus 4.8 only on hard/failed registration reasoning. Model choice is a per-step policy decision, not a global constant.

2. **Engineer the browser loop down — it is the dominant cost center.** Cap steps per registration; prune screenshot history to the last 1-3 frames (each 1280x800 frame ~1,334 tokens accumulates every turn); prefer DOM/`get_page_text` over screenshots; downscale to standard resolution (≤1568px) unless high-res is required. Budget ~$0.50-3 per browser registration; alarm on runs exceeding a step ceiling.

3. **Make prompt caching mandatory and structural.** Cache the shared system prompt, tool schemas, and per-user policy/preferences (cache read = 0.1x price AND excluded from ITPM). Order prompts stable-prefix-first so the large invariant block is a single cache breakpoint reused across every tenant request — both a cost cut and a throughput multiplier against org-level rate limits. Remember cache *writes* still consume ITPM.

4. **Cross-tenant discovery/extraction cache** keyed by (source, event_id) with TTL to event start — one `web_fetch` + Haiku parse amortized over every interested user. Never re-extract the same event per user. Prefer per-source API/feed adapters over browser discovery wherever a source exposes one.

5. **Ports-and-adapters with API-preferred, browser-fallback behind one port** for every source. Route through the API adapter whenever available (< $0.05 vs $0.5-3, 20-60x cheaper). Track per-adapter success rate and cost since failed browser attempts still burn tokens.

6. **Global rate-limit/quota scheduler** in front of BOTH Anthropic (org-level tiers) and third-party APIs (Eventbrite ~2,000/hr): per-tenant concurrency caps, fair-share queue, per-source budgets. Execute registrations using each user's own stored third-party credentials where possible so quota draws against the user's account, not one shared app token. Provision against Browserbase concurrency caps (25-100).

7. **Split the pipeline by latency tolerance.** Run discovery and ranking through the Batch API (50% off, stacks with caching) on Haiku/Sonnet; keep only the time-critical registration loop synchronous. Roughly halves the cost of high-volume upstream stages.

8. **Instrument per-request unit economics from day one.** Log tokens (input/cache-read/cache-write/output), step count, screenshots, browser-seconds, and model per lifecycle stage (found -> registered -> scheduled); compute cost-per-successful-registration. Set pricing/limits (per-user monthly registration quota or per-successful-registration fee) against the browser-heavy worst case, not the API-path best case.

9. **Do not over-optimize browser infra.** At ~$0.10-0.12/browser-hour (or $0.08/session-hr on Managed Agents), a registration session is sub-cent and a rounding error next to tokens. Choose Managed Agents vs self-hosted Agent SDK + Browserbase on isolation/ops grounds. Spend engineering effort on token reduction (fewer steps, caching, cheaper models, image pruning).

## Sources

- [Anthropic Pricing — official Claude Platform Docs](https://platform.claude.com/docs/en/about-claude/pricing) — Primary source. Verified full model price table, cache multipliers (0.1x/1.25x/2x), batch 50%, computer-use overhead (466-499 sys + 735/tool def), Managed Agents $0.08/session-hr, web search $10/1k, web fetch free, ~30% tokenizer inflation note. Also confirms code-execution containers $0.05/hr.
- [Prompt caching — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) — Cache read/write mechanics, workspace-level isolation (Feb 5 2026), automatic vs explicit breakpoints.
- [Anthropic Rate limits — Claude Platform Docs](https://platform.claude.com/docs/en/api/rate-limits) — Org-level RPM/ITPM/OTPM with token-bucket refill; explicit "Cache-aware ITPM" section (cache reads excluded; 2M ITPM @ 80% hit = 10M effective); Haiku 3.5 exception.
- [Vision — Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/vision) — ceil(w/28)*ceil(h/28) visual-token formula; high-res tier 2576px/4784 tok vs standard 1568px/1568 tok; worked counts (1000x1000=1296, 1920x1080=2691 high-res / 1560 standard).
- [Browserbase Pricing (official)](https://www.browserbase.com/pricing) — $0.10-0.12/browser-hr, 25-100 concurrent, plan tiers, proxy $8-12/GB; establishes browser infra is sub-cent per registration.
- [Browserbase plans & pricing docs](https://docs.browserbase.com/guides/plans-and-pricing) — Corroborates browser-hour and concurrency figures.
- [Browserbase Pricing blueprint (UsagePricing)](https://www.usagepricing.com/blueprint/browserbase) — Corroborates browser-hour overage and proxy bandwidth figures.
- [LLM API Rate Limits 2026 (Requesty)](https://www.requesty.ai/blog/rate-limits-for-llm-providers-openai-anthropic-and-deepseek) — Secondary: org-level RPM/ITPM by tier; cached tokens excluded enabling 5-10x throughput.
- [Eventbrite API Rate Limits (official)](https://www.eventbrite.com/platform/docs/rate-limits) — Third-party quota anchor (~2,000 calls/hr, 48,000/day per token) for multi-tenant quota budgeting.
- [OSWorld 2.0 — computer-use step-budget benchmark (arXiv)](https://arxiv.org/html/2606.29537v1) — Step counts scale with difficulty (leaderboards to 100 steps; hard tasks 301-450 step bins), informing per-registration step-budget assumptions.
- [Claude cost optimization 2026 — batch + caching stacking (PE Collective)](https://pecollective.com/tools/claude-pricing-guide/) — Secondary: confirms batch (50%) and caching (90%) stack to ~95% off for non-urgent workloads.

_Note: the morphllm.com "AI Coding Costs" page originally cited for per-run token math is omitted as load-bearing — the 25/100-step figures are fully derivable from Anthropic's own pricing, and the 100-step figures were found to assume ~80% (not 75%) cache hit._
