# Autonomous Inbound-Email Ingestion for Login Codes and RSVP Confirmations

> Closes the post-wave-1 gap of how the concierge autonomously completes magic-link / OTP login and captures registration confirmations across Luma, Eventbrite, Meetup, and Partiful — without taking Gmail restricted scopes, and without letting untrusted email into the agent loop.

## Verified findings

### Per-source auth reachability by email [confirmed]
Luma and Eventbrite deliver a login code by **email** and are therefore reachable by a mail relay; Partiful is **SMS-only** and is not reachable by any email path; Meetup is password/social-primary and emails single-use codes only for device/login verification.

- **Eventbrite** — passwordless flow: enter email, "Sign in with one-time code," 6-digit code emailed from `noreply@order.eventbrite.com`, valid 15 min, locks after 5 wrong attempts. Also supports Google/Facebook/Apple SSO + password. Order confirmations also originate from `noreply@order.eventbrite.com` (useful for a sender allowlist).
- **Luma** — passwordless: sign in with email **or** phone via a one-time verification code; one-click re-auth for returning guests. Note: current Luma help describes a **6-digit email code** rather than a "magic link" specifically, and Luma also supports SMS and passkeys, so it is not strictly email-only. The ingestion-relevant fact (a code arrives by email) holds. [adjusted]
- **Partiful** — phone number + SMS verification code **exclusively**; help center states guests "will still need to log in with their phone number in order to RSVP, even if they were invited via email." No email auth path exists; an email relay cannot cover Partiful. [confirmed]
- **Meetup** — email+password or Google/Apple/Facebook SSO primary; single-use codes emailed for login/device verification ("only the most recent code will work"), plus an address-confirmation email at signup. Note: the researcher framing "only Luma and Eventbrite put a code in email" is slightly loose since Meetup also emails single-use codes — but scoped to verification-only, the categorization is internally consistent. [adjusted]

### All content-reading Gmail scopes are Restricted [confirmed]
Per Google's official Gmail API scopes page, `gmail.readonly`, `gmail.modify`, **and** `gmail.metadata` are all **Restricted**; only `gmail.labels` is Non-sensitive and `gmail.send` is Sensitive. There is no cheap read path — even reading message metadata (headers/labels) requires a restricted scope. This refutes the common blog claim that "gmail.modify is Tier 2 sensitive"; it is Restricted.

Restricted scopes require OAuth app verification plus a **CASA** security assessment via Google-empanelled third-party assessors, re-assessed at least every **12 months** from the Letter of Assessment date. For restricted Gmail scopes the **Tier 2 self-scan is no longer offered** — an authorized lab (e.g. TAC Security, Leviathan, DEKRA, Bishop Fox) must run the DAST. Reading the user's primary inbox therefore imposes a recurring compliance + audit burden and a large credential blast-radius (the token exposes the user's entire personal mailbox).

- **Pricing is indicative, not authoritative [unverifiable].** Secondhand figures conflict: one source lists ~$540–$1,800 (Tier 2 DAST) / ~$4,500 (Tier 3 pentest); another surfaces an ADA-tiered Tier 2 schedule of ~$3,000 / $4,500 / $6,000. Treat all dollar amounts as marketing numbers. The 1–4 week turnaround and annual-renewal cadence are directionally right; the structural conclusion stands.

### Per-user relay inbox (Option 2) sidesteps Gmail scopes entirely [confirmed]
A per-user dedicated concierge relay inbox avoids the Gmail restricted scope, CASA, and the primary-inbox blast-radius, and is buildable free/cheap:

- **Cloudflare Email Routing** — supports RFC 5233 subaddressing (added 2025-07-21; `user+tag@` matches the `user@` rule and the tag is preserved in `message.to`) and subdomain routing; **Email Workers** parse each inbound message on the free tier. Native subdomain **catch-all** typically leans on an Email Worker rather than a checkbox.
- **AWS SES inbound** — receipt-rule recipient conditions match domains, subdomains, or addresses (a condition on `example.com` also covers its subdomains); the S3 raw-MIME (up to 40 MB) → SNS/Lambda chain enables programmatic parsing. **Region constraint (precise):** the SNS topic and KMS key must sit in the same region as the SES endpoint; the **S3 bucket is the explicit exception** (allowed cross-region via an IAM role). SES inbound is available only in certain AWS regions.

Because WE own the domain and inbox, no third-party OAuth token is stored and no CASA/verification applies. This makes Option 2 the recommended primary architecture for Luma/Eventbrite/Meetup email codes.

### Unique subdomain addressing over plus-addressing (medium confidence)
Both Cloudflare and SES support plus-addressing and per-address/subdomain catch-all. Plus-addressing (`base+tag@`) risks rejection at signup validators and is trivially fingerprinted as disposable; a unique full localpart or per-user subdomain (catch-all `*@u.concierge.app` → Worker keyed by localpart) looks like an ordinary distinct mailbox and cleanly maps one inbound address → one tenant. Documented evidence of plus-address rejection specifically at Meetup/Eventbrite was thin — treat per-platform acceptance as an integration-test item — but the unique-address scheme removes the risk architecturally.

### Treat inbound email as untrusted; extract deterministically [confirmed]
Email is a first-class indirect prompt-injection vector. OWASP LLM01:2025 defines indirect injection as arising "when an LLM accepts input from external sources, such as websites or files," and prescribes exactly these mitigations: separate/denote untrusted content, apply string-checking/semantic filters, restrict model privileges, and "handle these functions in code rather than providing them to the model." Immersive Labs demonstrated a working attack hiding instructions in non-visible HTML email elements that bypasses email-security gateways, warning that for autonomous agents "a user's simple request to summarize an email could trigger a full system compromise."

Recommended pattern: the ingestion subsystem deterministically extracts a minimal artifact (OTP code or magic-link URL) and passes **only** that artifact + minimal metadata (sender, subject, timestamp) to the agent. Sanitize/strip HTML before any LLM sees it; enforce per-source sender-domain allowlists (e.g. accept Eventbrite codes only from `order.eventbrite.com`) to defeat spoofed confirmations; treat the link/OTP as a secret (never log it, short TTL, honor one-time-use).

- Caveats: the "15-min expiry" is illustrative — magic-link/OTP lifetimes vary (commonly 5–60 min); honor each provider's actual expiry. Prefer conservative keyword + length-constrained extraction over brittle regex — still deterministic, non-LLM extraction. [adjusted]

### Durable coordination via Temporal signal raced against a timer [confirmed]
When the browser worker reaches the code/link entry screen it awaits a Temporal signal; the mail-ingestion Lambda/Worker, on extracting the artifact, calls `signalWorkflow(workflowId, {code})` on that (user,event) workflow. Signals are durable (recorded as `WorkflowExecutionSignaled`) and replay-safe even if a worker crashes between receipt and processing, so an email arriving **before** the browser reaches the page is buffered, not lost. A Temporal selector/condition races the signal against a durable timer set to the code's TTL so a non-arriving/expired code deterministically triggers retry-or-human-handoff rather than hanging. `workflowId` as idempotency key prevents a duplicate confirmation email double-advancing found→registered→scheduled.

### Confirmation links + non-email escalation (high confidence, non-load-bearing)
Some sources require an address **confirmation click** at signup (Meetup) alongside RSVP-receipt emails (Eventbrite/Luma), so the extractor must recognize verification **links** in addition to numeric OTPs, and the browser worker must open a captured link. Partiful's SMS-only auth means the relay cannot cover it — either provision a parallel per-user SMS-ingestion number (Twilio, same signal/timer coordination) or route Partiful to human-handoff, off the reliability-SLA critical path.

### Inbound-vendor persistence tradeoff (medium confidence)
- **Postmark inbound** (Pro/Platform) — POSTs pre-parsed JSON (full body, headers, spam score, Base64 attachments); retains 45 days by default, configurable 7–365. Least code, useful audit trail. Fastest MVP path.
- **Mailgun Inbound Routes** — MIME-parsed webhooks, strong routing-rule engine, **no persistent storage**.
- **AWS SES** — raw MIME to S3 + SNS/Lambda; maximal control and a durable, auditable raw-email store under your own keys (fits KMS-envelope / tenant-isolation posture), at the cost of writing the MIME parser and retention lifecycle. Cleanest fit for an auditability-first, tenant-isolated product.

## Design implications

### Per-source ingestion matrix

| Source | Auth channel | Email-relay reachable | Concierge strategy |
|---|---|---|---|
| Eventbrite | 6-digit email code (`noreply@order.eventbrite.com`, 15-min TTL, 5-attempt lockout) + SSO | Yes | Option 2 relay + deterministic OTP extract → Temporal signal |
| Luma | Email OR phone one-time code (+ passkeys/SSO) | Yes (email code) | Option 2 relay; prefer email code path |
| Meetup | Email+password / SSO primary; single-use email codes for device verify; signup confirmation link | Partial (verification/confirmation only) | Relay handles verification codes + confirmation LINK clicks |
| Partiful | SMS-only (phone login required to RSVP) | No | SMS-ingestion number (Twilio) OR human-handoff, off-SLA |

### Directives for our system
1. **Adopt Option 2 (per-user concierge relay inbox) as PRIMARY.** Users sign up on Luma/Eventbrite/Meetup with a unique concierge address (`alice@u.concierge.app` via subdomain catch-all), never their real inbox. Keeps the product entirely off Gmail restricted scopes / CASA and shrinks blast-radius to one per-tenant mailbox.
2. **Do NOT take `gmail.readonly` / `.modify` / `.metadata`** — all Restricted, all trigger CASA (lab-run DAST/pentest, indicative $540–$6,000, annual re-assessment) plus a token exposing the whole personal inbox. Keep Option 1 (Gmail read) and Option 3 (user-side forward-only filter) as opt-in fallbacks **only** for pre-existing registrations the user already made with their real address, explicitly flagged as outside the CASA-free happy path.
3. **Build an `EmailIngestionPort`** as a distinct adapter behind the same ports-and-adapters seam as `SourcePort`: SES-inbound → encrypted per-tenant S3 → Lambda that (a) allowlists sender domains per source (`order.eventbrite.com`, Luma domains, `meetup.com`), (b) deterministically regex/DOM-extracts the OTP or magic-link, (c) NEVER forwards raw HTML to the Claude loop, (d) emits a Temporal signal to the (user,event) workflow.
4. **Model login/confirmation as a Temporal workflow** blocking on a signal raced against a durable timer set to the code's real TTL. Buffering makes an early email safe; timer expiry routes deterministically to retry-or-human-handoff. Use `workflowId` as idempotency key.
5. **Treat the extracted link/OTP as a secret:** in-memory or short-TTL encrypted store, never logged, one-time-use and expiry honored, audit only redacted metadata. Sanitize/strip HTML and enforce per-source sender allowlists to block spoofed confirmation emails carrying injection payloads.
6. **Prefer unique full-localpart / per-user-subdomain addressing over plus-addressing;** add per-platform signup-acceptance as an integration-test item.
7. **Handle confirmation LINKS in addition to numeric OTPs** (Meetup signup verify, click-to-verify), and provision a non-email escalation for Partiful (Twilio SMS-ingestion number sharing the identical signal/timer coordination, or human-handoff).
8. **Vendor choice:** Postmark inbound for MVP speed (pre-parsed JSON, 7–365d retention); SES-inbound → encrypted S3 (per-tenant prefix, KMS envelope) → Lambda for the production auditability/tenant-isolation posture.

## Sources
- [Gmail API OAuth scopes (official)](https://developers.google.com/gmail/api/auth/scopes) — proves readonly/modify/metadata all Restricted; labels non-sensitive, send sensitive.
- [Google Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) — restricted scopes require CASA re-assessment ≥ every 12 months.
- [App Defense Alliance CASA Tier 2 overview](https://appdefensealliance.dev/casa/tier-2/tier2-overview) — corroborates lab-run DAST for restricted scopes; alternate tier pricing.
- [Google CASA 2025 costs/timelines (deepstrike)](https://deepstrike.io/blog/google-casa-security-assessment-2025) — indicative pricing; self-scan no longer offered for restricted Gmail.
- [Eventbrite account security / passwordless code](https://www.eventbrite.com/help/en-us/articles/344271/keeping-your-account-secure-on-eventbrite/) — 6-digit code, `noreply@order.eventbrite.com`, 15-min TTL, 5-attempt lockout.
- [Eventbrite order-confirmation sender](https://www.eventbrite.com/help/en-us/articles/642937/) — confirms `noreply@order.eventbrite.com` origin for allowlist.
- [Luma sign-in](https://help.luma.com/p/signing-in) / [Luma passkeys](https://help.luma.com/p/passkeys) — email-or-phone one-time code, passkeys, SSO.
- [Partiful RSVP / phone login](https://help.partiful.com/hc/en-us/articles/34230743189787-How-do-I-RSVP-to-an-event-on-Partiful) — SMS-only, phone login required to RSVP even if invited by email.
- [Meetup verification codes](https://help.meetup.com/hc/en-us/articles/4413591410957-Why-am-I-being-asked-to-enter-a-verification-code) — single-use email codes for device/login verification.
- [Cloudflare Email Routing subaddressing changelog (2025-07-21)](https://developers.cloudflare.com/changelog/post/2025-07-21-subaddressing/) — RFC 5233 tag preserved in `message.to`.
- [Cloudflare Email Routing subdomains](https://developers.cloudflare.com/email-routing/setup/subdomains/) + [addresses](https://developers.cloudflare.com/email-routing/setup/email-routing-addresses/) — subdomain routing, Email Workers, free tier.
- [AWS SES email-receiving concepts](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-concepts.html) — wildcard subdomain matching; region-colocation constraint.
- [AWS SES deliver-to-S3 action](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-action-s3.html) — raw MIME to S3, Lambda trigger.
- [AWS SES: manage incoming emails](https://aws.amazon.com/blogs/messaging-and-targeting/manage-incoming-emails-with-ses/) — S3+SNS+Lambda chaining pattern.
- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — indirect injection via email/web; deterministic in-code handling.
- [Immersive Labs: weaponizing LLMs via email injection](https://www.immersivelabs.com/resources/c7-blog/weaponizing-llms-bypassing-email-security-products-via-indirect-prompt-injection) — hidden-HTML injection bypassing gateways.
- [Mailhook: handling email verification codes at scale](https://mailhook.co/blog/email-address-verification-handle-codes-at-scale) — deterministic extraction, hostname allowlists, secret hygiene.
- [Temporal: handling messages](https://docs.temporal.io/handling-messages) / [Python message passing](https://docs.temporal.io/develop/python/message-passing) — durable signals, selector races with timers.
- [Inbound email vendor comparison (SES/Postmark/Mailgun)](https://emailsendx.com/blog/amazon-ses-vs-sendgrid-vs-mailgun-vs-postmark-2026) — retention/persistence tradeoffs.
