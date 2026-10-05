# Empirical gates

Retained harnesses for the deferred concierge scope; they are not the discovery task queue. Current scope is in [PROJECT.md](../PROJECT.md#current-milestone-private-discovery-candidate). Field inputs and owner review are required to close a gate; synthetic results alone cannot.

| Gate / runbook | Validates | Required input |
| --- | --- | --- |
| [G1: request mix](g1-request-mix/RUNBOOK.md) | Realized autonomous/browser/handoff mix and capacity assumptions | Consented representative corpus and provisioned account |
| [G2: Meetup RSVP](g2-meetup-rsvp/RUNBOOK.md) | Live join behavior, quota scope, retry semantics and consumed points | Approved Meetup Pro OAuth consumer, token and test group/event |
| [G3: inbound address](g3-relay-acceptance/RUNBOOK.md) | Signup-validator acceptance and code delivery/parsing | Approved inbound domain and mailbox/SES route |

- Preserve source-specific consent, request budgets, data classification and sanitized evidence.
- G2 gates autonomous-lane SLA and pacer/detection sizing. G1 informs capacity/Ticketmaster reserve; G3 gates the per-user inbound login/confirmation path.
- Do not enable deferred registration or email by running a harness. Credentials, live provider calls and deployment require their own authorization.
- [Requirements](../design/requirements.md), [owner review record](../design/owner-decisions.md) and [ADR history](../decisions/README.md) retain the original scope and unresolved riders.
