# Research Brief Addendum — Gap Remediation (wave 2)

> Wave 2 closes the design-endangering gaps a completeness critic raised after wave 1, under the hardened
> scope: multi-tenant, **free-RSVP-only launch**, API-first / browser-best-effort. Every claim cites the
> dossier it comes from (`01`–`17`). This addendum does not relitigate wave-1 architecture; it settles the
> open questions, corrects wave-1 claims 2026 re-verification changed, and marks what is closed vs deferred.

## Scope refinement applied, and what it removed from the risk surface

The refined scope — **autonomous free RSVP only**, paid purchase deferred behind policy flags — deletes the
largest risk cluster wave 1 researched. The payment port (ACP `delegate_payment` / AP2 mandates) survives
**only as a forward-looking abstraction behind a `paid_allowed` flag**, not a launch build item. Removed from
the launch risk surface (`04-autonomous-action-safety`, `02-autonomous-browser-registration`,
`03-event-source-landscape`): the federal **BOTS Act** exposure on automated purchase against the
Ticketmaster/Live Nation family; the **3-D Secure / SCA step-up gap** an autonomous charge cannot answer;
default-**merchant chargeback liability** under 2026 rules; and vault-token / signed-mandate machinery. The
**API-first / browser-best-effort** split partitions reliability: API-clean paths (Ticketmaster/SeatGeek
discovery, Meetup register) are SLA-guaranteed; browser-only sources (Luma, Partiful, Eventbrite) are
best-effort with human handoff, structurally **off the reliability-SLA critical path**
(`10-per-source-legal-tos`, `11-source-tier-reverify-2026`).

## Per-source decision matrix — the definitive launch source list

| Source | discover() | register() | Act-on-behalf auth | Rate limit | ToS posture | Ship-enabled at launch? |
|---|---|---|---|---|---|---|
| **Meetup** | API (GraphQL) | **API only** (`createEventRsvp`) | Per-user OAuth 2.0 auth-code; `member_id` omitted | 500 points/60s | Sanctioned; Pro-gated consumer; revocable; commercial-use clause | **Yes, on SLA — but only groups the user is already in** (see below) |
| **Ticketmaster** | API (Discovery v2) | n/a (read-only) | App-level `apikey` (shared) | 5,000/day, 5 rps, `size*page<1000` | Sanctioned discovery; no on-behalf action | **Yes (discovery)** |
| **SeatGeek** | API (partner-gated) | n/a | Client ID via Partner Program | Partner-set | Sanctioned, manual approval | Feature-flag; not launch-critical |
| **Bandsintown / Songkick** | API (read-only) | n/a | `app_id` / paid license | Vendor-set | Read-only; Songkick rejects hobbyist | Off launch floor |
| **SerpApi `google_events`** | API (cross-source funnel) | n/a | Product key | ~$0.025/search | Sanctioned metadata funnel | **Yes (funnel)** |
| **Luma** | browser / SerpApi (no search API) | browser (Computer Use + DOM) | Per-user session cookie; email-OTP via relay | 500 GET + 100 POST / 5 min / calendar | GREY — no user-side API; "publicly supported interfaces" clause | Best-effort, **off SLA**, human handoff |
| **Eventbrite** | browser / SerpApi (search API dead 2019) | **none shippable** — see below | (API §4.2 write-consent but no order endpoint); email-OTP via relay | 1,000/hr/token (read/manage) | API write sanctioned but no endpoint; consumer ToS §13.1 bans browser | **Discovery only**; register **deferred** |
| **Partiful** | browser (no API) | browser (DOM) | Per-user session; **SMS-only login** | n/a | PROHIBITED — bans robots + IP-block circumvention | **Disabled by default** |

**Meetup reconciliation (the biggest wave-2 downgrade).** `17-meetup-rsvp-prerequisite-chain` shows
`createEventRsvp` is **not** a standalone register: RSVP is **membership-gated** ("only members of your group
can attend"), there is **no `joinGroup`/`createProfile` mutation** in the GraphQL schema (self-service join is
not API-automatable), and real groups impose organizer-configured **approval queues, screening questions, and
paid dues** — each a human/payment gate. Downgrade the on-SLA claim from "all Meetup events" to **events in
groups the user is *already* a member of**; open, no-question, no-dues, instant-join groups are conditional
upside **only if a build-time spike** confirms `createEventRsvp` auto-joins. Approval/screening/dues groups are
human-handoff, off SLA. The single Pro-gated OAuth consumer is also a revocable, commercial-use-restricted
**single point of failure** for the only on-SLA register path — failures must degrade to human-handoff, never
cascade to an SLA breach.

**Eventbrite reconciliation.** `10-` finds API §4.2 contractually *sanctions* consented write-on-behalf, but
`11-` confirms Eventbrite exposes **no endpoint to create a free order/registration** (Orders API is
read/manage-only); consumer ToS §13.1 simultaneously bans browser automation. Net: Eventbrite ships
**discovery-only**, `register()` **deferred to human handoff**, never a ToS-violating browser POST. **Launch
autonomous-register floor = Meetup (API, on SLA, member-scoped) + Luma (browser, best-effort, off SLA).**
Discovery floor = Ticketmaster + Meetup + SerpApi. `automation_allowed` is **per-source, per-modality**
(`{api, browser}`); the `SourcePort` selector must *refuse* the browser adapter for API-sanctioned-only sources
(Meetup, Eventbrite) (`10-`). Binding legal risk is contract + trespass-to-chattels + account/IP bans (hiQ's
$500k non-CFAA judgment), **not CFAA** (Van Buren) — enforced via per-user rate-limiting, human-cadence pacing,
and a **quarantine-on-ban circuit-breaker**, never IP-rotation or block-circumvention (`10-`).

## Durable-execution engine decision + saga / idempotency spine

**Temporal Cloud is the durable spine; DBOS is the documented lighter fallback**
(`13-durable-execution-engine`). Temporal fits a multi-tenant product with SLAs and long-lived, signal-driven
per-entity workflows; Cloud dodges the three-service self-host burden. Its one tax — the hard **2 MB payload /
4 MB gRPC limit** — is paid down day one with the **claim-check pattern**: screenshots, raw DOM, and candidate
lists go to object storage, only the key travels in payloads. One workflow per `(user_id, canonical_event_id)`;
`workflowId = f"{user_id}:{event_id}"` with a **reject-duplicate `WorkflowIdReusePolicy`** as the engine-level
idempotency key. Saga: `discover → rank → resolve_membership → register_or_rsvp → await_confirmation →
dedupe_calendar → write_to_calendar`, each side-effecting step its own journaled Activity. Meetup adds an
explicit **`needs_membership → (auto_joinable | human_handoff)`** precondition branch before RSVP (`17-`) —
never assume RSVP auto-joins. Activities are **at-least-once**, so effective exactly-once needs idempotent
activities plus a key **minted once in workflow state and passed in** (never generated inside a retried
activity). The confirmation/login-code wait is a **durable timer + signal** (`wait_condition` on a Signal,
`sleep` timeout → human handoff), zero worker resources — replacing wave 1's blocking-agent-turn anti-pattern.

**Browser-path re-query rule (new).** Engine idempotency guarantees effectively-once invocation *from our
side*, but a browser RSVP POST to Luma/Partiful carries **no server-honored idempotency key**, so a retry after
a lost ACK can double-RSVP. The browser register Activity splits into **detect-then-submit**: on every retry
re-run a read-only detect (scrape the "You're going" marker / RSVP history). CONFIRMED → idempotent no-op;
NOT_PRESENT → safe to submit; AMBIGUOUS (timeout, layout change, login wall) → durable signal to human review,
**never a blind re-POST** (`13-`). **Correction:** wave 1's "LangGraph has no mechanism to prevent side-effect
re-execution" was inaccurate — `@task`-wrapped completed results *are* replayed; the real gaps (plain nodes
always replay; no same-`thread_id` coordination lock) still justify Temporal (`13-`).

## Google / calendar OAuth & CASA Tier 2 launch plan

**A calendar-only Google scope set dodges CASA Tier 2 entirely** (`12-google-oauth-casa-gate`). Google's
authoritative restricted-scope list enumerates only seven APIs (Gmail, Drive, Fitness, Chat, Data Portability,
Photos, Health) — **Calendar is absent**, so `calendar.events` (write) + `calendar.freebusy` (dedup/conflict
gate) are **sensitive-tier, not restricted**, and never trigger third-party DAST / annual recert / Letter of
Validation. This **corrects wave 1's `05-multitenancy-auth-secrets` claim** that Calendar scopes force CASA.
Calendar-only still forces **standard sensitive-scope app verification** — a real critical-path gate:
brand/consent (~2–3 business days) then sensitive-scope review (nominal "up to 10 days," **plan 3–6 weeks
wall-clock**). It **cannot ship on Testing status** (calendar refresh tokens expire in 7 days there);
publishing to Production is mandatory and forces verification, so "submit for verification" is a **pre-launch
milestone**. Minimize with the `calendar.app.created` app-owned secondary-calendar pattern + **incremental
authorization** (`include_granted_scopes=true`), requesting write only at the scheduled-to-calendar step. Store
**exactly one refresh token per user** (the 100-token silent-invalidation cap is **per-our-client-ID only** —
corrects wave 1's "fleet + user's other apps" framing).

**Holding the calendar-only line requires ingesting login codes without Gmail scopes** (`16-email-ingestion-
architecture`). All content Gmail scopes (`readonly`/`modify`/`metadata`) are **Restricted** — reading the
inbox to capture Luma/Eventbrite OTPs would flip the whole client into restricted + recurring CASA. Instead,
adopt a **per-user concierge relay inbox** as PRIMARY: users register with a unique address
(`alice@u.concierge.app`, SES-inbound → encrypted per-tenant S3 → Lambda). A distinct **`EmailIngestionPort`**
allowlists sender domains (`order.eventbrite.com`, Luma, `meetup.com`), **deterministically** extracts the OTP
/ magic-link (never forwarding raw HTML to the Claude loop — email is a first-class indirect-injection vector,
OWASP LLM01), and emits the Temporal signal above. Partiful is **SMS-only** → Twilio SMS-ingestion number on
the identical coordination, or human-handoff (`16-`). This keeps the product off Gmail restricted scopes — the
bright red roadmap line. Microsoft Graph needs only Publisher Verification; Apple iCloud is CalDAV +
app-specific password, off SLA.

## Credential-injection build-vs-buy + browser-worker isolation & lifecycle

**Decision: BUILD a self-hosted KMS-envelope injection worker; do not buy 1Password for the unattended path**
(`14-credential-injection-worker-isolation`). 1Password Secure Agentic Autofill is human-in-the-loop by
architecture (live biometric tap per login, Browserbase-Director-pinned) with no unattended auto-approval; its
only headless path (Service Account SDK token) re-exposes plaintext to the agent process and collapses
per-tenant isolation into one blast radius. Build pattern: a **non-LLM placeholder/reference broker** — the
Claude loop emits `{{username}}`/`{{password}}` tokens; the broker unwraps the per-tenant KMS-enveloped DEK,
decrypts into a locked buffer, verifies the live URL by **domain-pinning** (CDP `currentURL` == bound origin),
and types via Playwright `fill()`. The secret is **structurally excluded** from prompts, tool-args, Temporal
payloads, and logs. Isolation: **one ephemeral Firecracker (or gVisor) microVM per `(user,event)` login
session** (~125 ms boot, <5 MiB overhead), no cross-tenant coresidency, behind a **mandatory egress proxy**
allowlisting only the target event origin + KMS. A **CaMeL / User-Alignment-Critic dual-LLM split** keeps
untrusted page content (indirect prompt injection) — and, identically, the extracted OTP/magic-link — out of
the injection and action decision. **Zeroize** plaintext in `mlock`'d, `MADV_DONTDUMP` guarded memory
immediately after typing. Lifecycle: prefer **short-TTL session cookies over stored passwords**, reusing a
captured cookie only from the same worker identity (DBSC / device-binding sites reject cross-host replay);
store passwords only as re-auth fallback; a failed-login / MFA / CAPTCHA marks the credential `needs_reauth`,
**halts autonomous retries**, and emits human-handoff. **Prefer OAuth (Meetup, Google) so no password is
stored** — reserve injection for browser-only sources (Luma, Partiful). A first-class **consent record**
(per-source, per-user, per-scope, timestamped) satisfies Eventbrite §4.2, Meetup OAuth, and GDPR/CCPA
lawful-basis in one artifact (`10-`).

## Capacity plan vs per-source quotas and Anthropic limits — the real bottleneck

A **two-track source-scaling model** (`15-capacity-quota-model`):

| Regime | Sources | Lever | Rule |
|---|---|---|---|
| **Shared app-key** | Ticketmaster, SeatGeek | Central read-through catalog cache | Scheduled geo+category-sharded crawl → Postgres; ALL tenant discovery reads hit cache, never live per-user calls |
| **Per-user OAuth** | Meetup, Luma | Credential fan-out | Draw discovery + RSVP against each user's own stored token → quota is per-tenant, never one shared token |

**Ticketmaster's 5,000/day single global app-key budget bites first (~4–5k users on a naive path)** — before
Anthropic or browser concurrency (`15-`). Because TM events are tenant-invariant, a **central read-through
catalog crawl is the LAUNCH architecture, not a later optimization** (N×M user requests must not fan out to N×M
source calls); `size*page<1000` forces sharded crawling over pagination. **Caveat:** Meetup's 500-point budget
scoping (per-token vs per-app vs per-IP) is **undocumented** — validate before treating per-user tokens as the
quota escape hatch (`15-`, `17-`). Anthropic tiers are now **Start / Build / Scale / Custom** (corrects wave
1's "Tier 4"); **Scale = 10k RPM / 10M ITPM / 2M OTPM per model class**. Design so **ITPM/OTPM — not RPM — is
the constraint**: cache-read tokens are excluded from ITPM (~5× input headroom at 80% hit), and the **Batch API
is a separate ~50%-cheaper pool** off the synchronous budget for ranking + post-RSVP summarization. The
scarcest mid-scale resource is **Browserbase / Computer-Use concurrency** (~75–100k users), off the SLA with
human-handoff on saturation; self-host Chromium at ~100 concurrent. Unit economics: **~$0.10–0.20 per confirmed
RSVP** blended, gated by a per-tenant RSVPs/period policy limit (`15-`).

## Corrections / updates to the wave-1 brief

- **Meetup register modality, rate limit, and coverage (biggest change).** `03-` tiered Meetup as
  "management-only API + **browser** RSVP, ~200 req/hr." Wave 2 (`11-`, `15-`, `17-`): RSVP is **API-only** via
  `createEventRsvp` under per-user OAuth (browser prohibited), limit is **500 points/60s**, and coverage is
  **membership-gated** — downgrade from "all Meetup events" to "groups the user is already in." No
  organizer-key-RSVPs-strangers path (`member_id` omitted → 401 for non-hosts).
- **CASA Tier 2 trigger.** `05-` said calendar/Gmail scopes force CASA; `12-` corrects — calendar-only is
  **sensitive, not restricted**, and dodges CASA (still needs sensitive-scope verification). All content Gmail
  scopes are Restricted, so inbox reading is avoided via a relay inbox (`16-`).
- **100 refresh-token limit.** `05-`'s "fleet + user's other apps" → `12-`: the cap is **per-our-client-ID
  only**.
- **Anthropic tier scheme.** `09-`'s legacy numeric tiers → `15-`: **Start/Build/Scale/Custom**, Scale = 10k
  RPM / 10M ITPM / 2M OTPM **per model**.
- **LangGraph durability contrast.** `01-`'s "no mechanism to prevent side-effect re-execution" → `13-`:
  `@task` results *are* cached; real gaps are narrower (still justify Temporal).
- **Eventbrite.** Wave-1 posture implied API/paid RSVP; wave 2 (`10-`, `11-`): no order endpoint + browser
  ToS-banned → **discovery-only at launch, register deferred**.
- **Luma rate limit.** `03-`'s "200/min-calendar" → `11-`/`15-`: **500 GET + 100 POST per 5 min per calendar**.

## Gaps now CLOSED vs DEFERRED

**CLOSED by wave 2:** durable-execution engine → Temporal Cloud + DBOS fallback + saga/idempotency + browser
detect-then-submit (`13-`); credential-injection build-vs-buy → BUILD KMS-envelope broker + microVM isolation +
lifecycle (`14-`); CASA Tier 2 / calendar OAuth timeline + scope minimization (`12-`); autonomous OTP /
confirmation ingestion without Gmail scopes → per-user relay inbox + Temporal signal (`16-`); per-source
act-on-behalf auth, ToS posture, rate limits → definitive launch matrix (`10-`, `11-`, `17-`); capacity
bottleneck → TM central cache + two-track scaling + Anthropic Scale sizing (`15-`).

**DEFERRED (explicit):** **Per-site browser success-rate** stays a build-time **empirical spike** — the
~56–64% state-mutating ceiling (`02-`) is engineered against with detect-then-submit + human-handoff, but real
per-source rates (Luma, Partiful) are unknown until measured. A **blocking Meetup build-time spike** must
answer, before shipping the on-SLA promise: does `createEventRsvp` auto-join an open group, and is the
500-point quota scoped per-token (`17-`, `15-`). **Payment rails / SCA / chargeback liability** are deferred
with paid scope — the payment port stays a forward-looking ACP/AP2 abstraction behind `paid_allowed`, no launch
build (`04-`). Also deferred: SeatGeek/Songkick partner-gating sign-off (`11-`); the personalization-model
lifecycle (per-tenant vs shared LightGBM, cold-start→LTR blend) remains a wave-1 design-phase open question
(`07-ranking-personalization`).
