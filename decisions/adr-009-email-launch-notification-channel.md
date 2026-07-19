# ADR-009: Email to the real address as the launch notification channel; SMS stubbed behind the port

**Status:** accepted — with owner-ratification riders (see Consequences) · **Date:** 2026-07-10

## Context

The launch is UI-less, so notifications are not a convenience layer — they are the product's entire outbound surface, and two inbound contracts (un-RSVP and mark-done) ride on whatever channel carries them. FR-6.6 defines the contract: every user-facing notification (handoff task available, completion, reconcile, no-result) goes over a defined launch channel — email to the user's real address, or SMS — with at-least-once delivery, bounded retry, and dedup; it simultaneously forbids the RelayInbox from carrying any outbound traffic, because the relay is a system-controlled inbound mailbox the user does not monitor. The channel choice is bound by hard latency and composition constraints:

- **Latency:** NFR-2(b) — handoff task available p95 ≤ 60s and user notified p95 ≤ 120s from the routing decision, measured at *delivery, not enqueue*, with per-lane dashboards and notification-delivery latency from the channel itself.
- **Delivery semantics:** FR-6.6/AC-46 — at-least-once delivery with retry and dedup; an ignored task past its policy TTL reaches `expired`, closes its workflow, and notifies the user.
- **Inbound composition:** FR-6.7 (un-RSVP by reply to the notification), FR-6.8 (EventRequest intake), and the FR-6.3 mark-done confirmation signal — all specified now as stable contracts because the UI is deferred (NFR-16).
- **Relay separation:** FR-6.6 — the RelayInbox (`u.<domain>`, per d16) is structurally inbound-only.
- **Pre-launch clock:** NFR-12 — the Google sensitive-scope verification wait (plan 3–6 weeks wall-clock) is already on the critical path, and email-domain reputation warm-up has a comparable lead time.
- **Onboarding reality:** no phone number is collected at onboarding today; the SES estate already exists for the RelayInbox inbound pipeline.

## Options considered

1. **SMS (Twilio) as the launch channel.** FR-6.6 permits it, and SMS has a plausible latency edge for the 120s user-notified budget. Rejected because US A2P 10DLC carrier registration adds weeks of lead time for zero launch requirement; no phone number is collected at onboarding today; the reply-based inbound contracts (FR-6.7 un-RSVP, FR-6.8 intake) and deep links compose naturally on email threads and awkwardly on SMS; and the SES estate already exists for RelayInbox inbound, so email adds no new vendor. SMS survives as a stubbed adapter behind the NotificationPort with a documented channel-swap runbook, promoted if the measured email-delivery p95 ever breaches the NFR-2(b) budget.

2. **Outbound notifications from the RelayInbox domain.** Superficially attractive — one domain, one identity, and replies would land in a mailbox the system already parses. Rejected because FR-6.6 explicitly forbids it: the relay is an inbound channel for source login codes that the user does not monitor, and mixing outbound product mail into it couples the relay's inbound reputation (which the confirmation-matching and change-email channels depend on) to outbound sending behavior. The separation is enforced structurally, not by convention (see Decision).

3. **Email to the user's real address via SES v2 from a dedicated sending subdomain (chosen).** Meets every binding constraint with measured headroom (~40s p50 / ~90s p95 to SES-confirmed delivery against the 120s budget), composes the reply-based inbound contracts for free, and requires no new vendor or onboarding field. Its accepted weaknesses — hard-bounce blindness and the recipient-MTA delivery tail — are named as owner-ratification riders rather than papered over.

## Decision

We will deliver every user-facing notification as email to the user's real address, sent via SES v2 from `notify.<domain>` — a distinct SES identity and subdomain from the RelayInbox `u.<domain>` — with a config-level and CI lint check forbidding any outbound send from the relay domain (FR-6.6 enforced structurally).

- **Outbox consumer.** The Notifier is a transactional-outbox consumer (FR-8.9 substrate, per ADR-007) woken by Postgres LISTEN/NOTIFY with a 2s poll fallback, claiming rows `FOR UPDATE SKIP LOCKED` so a consumer crash mid-batch unlocks and redelivers without coordination.
- **At-most-once user-visible.** A notification ledger with UNIQUE(dedup_key = `workflow_id:type:transition_id`) converts the outbox's at-least-once delivery into at-most-once user-visible sends — redelivery after a crash no-ops on the ledger (FR-6.6 dedup, AC-46).
- **Bounded retry.** 5 attempts at 0/30s/2m/10m/30m (~43 minutes total envelope, chosen deliberately over a short envelope so a multi-minute SES incident still delivers), then `failed` + ops alert. Hard bounce → suppression list + ops escalation + account flagged notification-degraded.
- **Measurement.** SES configuration-set delivery events stamp `delivered_at`, which is the NFR-2(b) measurement point — delivery, not enqueue. Alerts fire at p95 > 45s task-available and p95 > 90s user-notified, preserving reaction headroom inside the 60s/120s budgets.
- **Inbound contracts on the same channel.** Every task notification carries a signed one-tap mark-done URL (HMAC token over task_id + tenant + expiry = task TTL; GET `/t/{token}/done` → `handoff_completed` signal with evidence=user_mark_done) satisfying the FR-6.3 explicit-mark-done floor; every notification sets Reply-To `act+<task_token>@in.<domain>`, parsed deterministically for DONE/CANCEL/UNRSVP to drive FR-6.7 without the deferred UI. Fuzzy completion matches (the FR-9.3 recipe) NEVER auto-complete — they attach evidence and send a one-tap confirm prompt; ambiguous evidence never advances state.
- **Channel abstraction.** All sends go through a NotificationPort; a Twilio SMS adapter ships as a stub behind the same port with a documented channel-swap runbook, making the channel a configuration change if email deliverability degrades.
- **Warm-up scheduling.** Sending-domain warm-up for `notify.<domain>` (DKIM/SPF/DMARC, reputation ramp) begins during the NFR-12 Google sensitive-scope verification wait, so the two pre-launch clocks overlap rather than serialize.
- **Launch parameters.** Retry 5 attempts / ~43min; alert thresholds 45s/90s; mark-done token expiry = task TTL (min(7 days, event start), per ADR-007); dedup key `workflow_id:type:transition_id`.

## Consequences

**Easier.** The reply-based un-RSVP and intake contracts (FR-6.7/6.8) come free on email threads; confirmation and organizer-change emails, deep links, and notifications all live in one medium the user already checks; no A2P 10DLC registration, no phone-number onboarding field, no new vendor; the outbox → ledger pipeline makes AC-46's at-least-once-with-dedup fixture a direct test of production machinery; a channel swap later is a port adapter plus runbook, not a redesign.

**Harder / risks accepted.** Email deliverability is the single most credible NFR-2(b) breach: a cold `notify.<domain>` hitting greylisting or reputation throttles pushes the delivery tail past 120s, so warm-up must complete pre-launch and per-provider delivery-latency SLOs must alert continuously. Email is the *only* launch channel, so a hard-bouncing address silences the user entirely while the task TTL keeps running. The signed one-tap mark-done URL becomes a launch requirement, not a nicety — if gate G3 (RelayInbox acceptance at source signup validators) fails, mark-done is the sole completion path for those sources, and its click-through rate must be instrumented from day one. The 45s/90s alert thresholds consume most of the SLA headroom, accepted in exchange for pre-breach paging.

**Follow-ups committed.** Begin `notify.<domain>` warm-up immediately, overlapped with the NFR-12 verification window; wire the CI lint forbidding outbound from the relay domain; implement onboarding address verification and bounce alerting before launch; instrument mark-done click-through and per-provider delivery-latency SLOs; write and rehearse the SMS channel-swap runbook against the stub adapter; feed unmatched-confirmation and expired-task volumes into the O-9 staffing decision.

**Owner-ratification riders:**
- OWNER RATIFY — email-only launch consequence: a hard-bouncing real address can reach TTL expiry without effective notice — the task and its TTL proceed while the user hears nothing. Accepted launch posture, mitigated by onboarding address verification plus bounce alerting (suppression + ops escalation + notification-degraded account flag), with the SMS adapter as the post-launch second channel.
- OWNER RATIFY — NFR-2(b) measurement boundary: "user-notified" is measured at the SES delivery event, the honest edge of our control; a greylisting or deferring recipient MTA can push the p99 tail past 120s and no architecture on our side removes that. Ratify the boundary together with the 45s/90s alert thresholds.
- OWNER RATIFY — fuzzy completion posture: a fuzzy confirmation match never auto-completes; it costs the user exactly one confirmation tap in exchange for zero wrong-task lifecycle advances (a wrong completion advances the lifecycle and writes a calendar entry for an event the user never registered for). Ratify as product behavior.
