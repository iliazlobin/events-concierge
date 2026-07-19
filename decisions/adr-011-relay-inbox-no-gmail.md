# ADR-011: Per-user RelayInbox for login codes and confirmations; permanent no-Gmail rule

**Status:** accepted — with owner-ratification riders (see Consequences) · **Date:** 2026-07-10

## Context

Passwordless browser-only sources (Luma foremost) log in and confirm via email magic-link/OTP, and organizer confirmations and change-emails are the highest-quality completion and detection signals the system can receive — but reading the user's own mailbox would drag a Gmail restricted scope, a CASA security assessment, and an unbounded blast radius into a product whose only necessary Google surface is the calendar. The requirements settle this: each user is provisioned exactly one concierge address `alice@u.<domain>` at onboarding, and every login code and confirmation arrives on a channel the system controls, never Gmail. The forces:

- **FR-1.5 / AC-4**: onboarding provisions exactly one RelayInbox address bound to the `tenant_id`.
- **FR-5.7 / AC-38**: OTP/magic-link ingestion is sender-domain allowlisted, deterministically extracted (raw HTML never reaches an LLM), and delivered as a durable workflow signal.
- **FR-5.8**: a magic-link/OTP is a short-TTL secret — never logged, never in a prompt or tool-arg, expired after use; **FR-2.6** structurally excludes every plaintext secret from prompts, tool arguments, Temporal history, and logs.
- **FR-10.6 / AC-75**: ingested email is an untrusted indirect-injection vector (OWASP LLM01) — allowlist senders, never forward raw content to a tool-holding LLM.
- **FR-9.1** lint-gates the Google adapter to calendar-family scopes only; **NFR-12** makes sensitive-scope verification (Production publishing, plan 3–6 weeks wall-clock) the pre-launch critical-path gate that calendar-only scopes buy.
- **FR-2.13 / AC-16** (account linking for browser-only sources) and **FR-6.3 / AC-43** (completion matching) both consume relay traffic; **FR-6.6** declares the RelayInbox inbound-only — the user does not monitor it, and it must never send.
- **Gate G3** (pre-design verification, still open): do Luma/Eventbrite/Meetup signup validators accept relay addresses at all?

## Options considered

1. **Gmail read scope on the user's real mailbox.** Rejected permanently. Gmail read is a Google *restricted* scope: it triggers a CASA security assessment on top of app verification, versus the sensitive-scope-only review that calendar-family scopes require — a categorical difference in launch timeline and standing compliance burden. It also makes the blast radius of any compromise the user's entire correspondence rather than event mail on a domain we own, and it puts untrusted third-party email one hop from the credential path. The no-Gmail rule is recorded as permanent and enforced mechanically: the CalendarPort adapter is lint-gated to hold no Gmail/Drive/Chat/Photos scope ever (FR-9.1).

2. **Plaintext OTP in the workflow signal** (relay ingestion, but `signalWorkflow(user:event, {code})` carrying the code itself). Rejected as a confirmed FR-2.6 violation: the plaintext OTP — a secret per FR-5.8 — lands in Temporal workflow history via the `WorkflowExecutionSignaled` payload, where it persists, replicates, and survives into exports. Any design that claims FR-5.7/5.8 satisfaction while signaling plaintext is defective; the signal must carry an opaque reference only.

3. **LLM-assisted extraction of codes/confirmations from relay email.** Rejected: FR-10.6 forbids raw inbound content reaching a tool-holding LLM, and an attacker-authored email is the canonical indirect-injection vector. Extraction is deterministic (allowlist, HTML strip, keyword+length rules); the only LLM contact with inbound mail anywhere in the system is a quarantined, tool-less structured extractor on the separate EventRequest intake path — never on the OTP/confirmation path.

4. **Per-user RelayInbox with claim-check secret handling** — accepted, as specified below.

## Decision

We will provision one RelayInbox address `alice@u.<domain>` per user at onboarding, bound to the tenant (FR-1.5), and route all source login codes, magic links, confirmation emails, and organizer change-emails through it. This was settled in research (d16) and requirements (FR-1.5, FR-5.7/5.8); this ADR ratifies the architecture:

- **Ingestion pipeline:** SES inbound → per-tenant KMS-encrypted S3 prefix → deterministic Lambda extraction: per-source sender-domain allowlist, HTML strip, keyword+length extraction — no LLM ever sees the raw email (FR-10.6).
- **Claim-check secrets:** the Lambda KMS-encrypts the extracted OTP/magic-link into the vault's ephemeral-secrets table (TTL = provider-documented expiry, consumed-once flag) and signals the workflow with only `{otp_ref, sender, expires_at}`. Only the Injection Broker — the sole `kms:Decrypt` principal — can redeem the ref, marking it consumed. Temporal history carries references, never secrets (FR-2.6, FR-5.8).
- **Race semantics:** the workflow races the signal against a durable timer (FR-8.4); signals arriving before the wait are durably buffered; expired rows are unreadable and garbage-collected; duplicate or spoofed emails are dropped by the allowlist, deduplicated by consumed-once refs and workflowId idempotency, and audited.
- **Structurally inbound-only:** the RelayInbox never sends. Outbound notification uses a distinct SES identity on `notify.<domain>`, with a config/CI lint forbidding any outbound send from the relay domain (FR-6.6).
- **Completion and detection reuse:** relay confirmation emails feed the Completion Matcher (exact source-event-id match auto-completes; fuzzy never does) and organizer change-emails accelerate change detection for Luma and handoff-lane events (FR-6.3, FR-8.7a); handoff deep links pre-fill the relay address where the source form accepts one (best-effort, gated on G3).
- **Google surface stays calendar-only:** `calendar.events` write + `calendar.freebusy`, sensitive-scope verification on the pre-launch critical path (NFR-12, 3–6 weeks) instead of restricted-scope CASA; the no-Gmail rule is permanent and lint-enforced (FR-9.1).
- **Threat posture:** email is an untrusted indirect-injection vector end-to-end (FR-10.6); magic-links/OTPs are short-TTL secrets (FR-5.8).

Invariants: (1) the RelayInbox is inbound-only, CI-enforced; (2) no raw inbound email reaches a tool-holding LLM; (3) no plaintext OTP/magic-link enters prompts, tool-args, Temporal history, rows, or logs — refs only, redeemable once, by exactly one principal; (4) non-allowlisted senders are dropped with an audit event; (5) erasure (FR-10.5/NFR-11, ≤72 h) purges RelayInbox contents and crypto-shreds ephemeral refs along with vault ciphertext.

Launch parameters: OTP ref TTL = provider-documented expiry; confirmation wait = 24 h durable timer racing the relay signal, timeout → handoff; sender allowlist per source (order.eventbrite.com, Luma domains, meetup.com); relay domain `u.<domain>` vs notify domain `notify.<domain>`; sending-domain warm-up (DKIM/SPF/DMARC) runs during the NFR-12 verification wait so the two pre-launch clocks overlap.

## Consequences

Easier: the Google OAuth burden drops to one sensitive-scope review — no CASA, no restricted-scope re-assessments, no Gmail data-handling obligations; compromise blast radius is capped at event mail on a domain we own and revoke; FR-2.6/FR-5.8 hold by construction rather than by convention (the CI secret-scan verifies an invariant the architecture already guarantees); confirmations and organizer change-emails arrive on a channel we control, giving near-real-time completion matching and change detection for sources with no webhook; erasure of relay contents is a first-party operation.

Harder: we now operate an email estate — SES inbound, deliverability and reputation for the paired outbound domain, per-source extractor maintenance when providers change templates (a template drift degrades extraction until patched); the user never monitors the relay, so when they registered via the relay the system's own re-notifications are their only channel for source-side changes; deterministic extraction refuses to guess, so unmatched confirmations create human ops work; SMS-only login (Partiful) is explicitly not served by the RelayInbox and needs no path at launch (FR-2.4, disabled by default per FR-5.6).

Risks accepted: G3 is open — the entire OTP/magic-link and account-linking leg is built on an unverified assumption that signup validators accept relay addresses; relay-domain reputation is a shared-fate asset across all tenants; a provider shortening OTP TTLs squeezes the race window toward handoff.

Follow-ups committed: resolve G3 before design freeze; instrument the signed one-tap mark-done URL's click-through from day one (it is the completion floor if relay channels fail); per-source extractor fixtures including injection payloads (AC-75); staffing answer for the unmatched-confirmations queue before launch volume.

**Owner-ratification riders:**
- **GATE G3 (pre-design verification, still open):** do Luma/Eventbrite/Meetup signup validators ACCEPT relay addresses? If rejected, the OTP/magic-link path has no in-scope fallback (Gmail is forbidden) — resolve via reputable dedicated domain / per-user real mailbox / first-login human-handoff fallbacks. G3 gates FR-2.13 account linking, whose sign-up-with-relay precondition it is.
- **Handoff users registering with their REAL email** (despite the relay prefill) silence both the confirmation-match and change-email channels for that (user, event): completion degrades to mark-done-only and detection to the public page. Such registrations are flagged **poll-only** and surfaced on the reconcile SLO dashboard; an invite-gated page (403) reduces detection to nothing — ratify this coverage posture.
- **Relay-prefill coupling:** when the user does register with the relay address, organizer emails route to a mailbox the user does not monitor and the user depends on our re-notifications for source-side changes — ratify the coupling (it is also what makes near-real-time change detection work).
