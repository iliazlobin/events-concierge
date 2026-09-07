# Events Concierge — empirical spike harnesses

Ready-to-run harnesses for the three empirical gates surfaced in the design phase. The code and
runbooks are complete; closing a gate still requires representative field inputs and owner review.
See `design/owner-decisions.md` Part C for how these gate the rest of the plan.

| Spike | Gate | Decides | Blocks | Needs from you |
|---|---|---|---|---|
| `g1-request-mix/` | **G1** (estimation) | Deployed eligible-lane mix for a realistic request corpus | Browser capacity, Ticketmaster reserve, and handoff staffing | A consented representative corpus, measurement cohorts, and a deployed provisioned account |
| `g2-meetup-rsvp/` | **G2** (design-blocking) | Meetup `createEventRsvp`: auto-join open groups? quota per-token/per-app/per-IP? retry-idempotent? real point cost? | Any autonomous-lane SLA; the ADR-005 pacer key; ADR-008 detection sizing | A Meetup Pro OAuth consumer + token(s), a test group/event |
| `g3-relay-acceptance/` | **G3** (pre-design verification) | Do Luma/Eventbrite/Meetup signup validators accept `alice@u.<domain>`, and does the code arrive + parse? | The whole OTP/magic-link login leg + FR-2.13 account linking | A relay domain with inbound email (IMAP mailbox or SES→S3) |

## Why these two, and why now

All three were carried out of research as empirical (not further-researchable) gates:
- **G1** measures the deployed source/routing mix without creating workflows or provider effects;
  corpus representativeness and cohort weights remain owner-reviewed evidence inputs.
- **G2** is the single design-blocking unknown — the requirements refuse to fix any autonomous-lane SLA number until it is measured (NFR-2(a)). Its answers flip concrete design switches (LaneRouter open-group row, the pacer's per-app config, detection poll budgets).
- **G3** is a precondition for the passwordless-source login path; a rejection with no working fallback would force a design change (mandatory first-login handoff for that source), so it must resolve before the login leg is built.

## Design posture

- Run against **your own accounts** through **sanctioned APIs / ordinary signup** — legitimate feasibility testing, not automation abuse.
- **G2** is dry-run by default, small-N, honors `X-RateLimit-Reset`/`Retry-After`, never rotates identity or bypasses a block.
- **G3**'s receiver is a faithful prototype of the FR-5.7/5.8 EmailIngestionPort: per-source sender allowlist, deterministic extraction, no raw HTML to any LLM, short-TTL secret handling. A passing run yields validated ingestion code, not just a yes/no.

## Running

Each subdirectory has a `RUNBOOK.md` (setup + procedure + how to interpret results into the design).
G2/G3 retain their standalone spike dependencies; G1 runs from the locked application environment.
Record every verdict + report path in `PROJECT.md`, and where a result diverges from a carried
assumption, open a superseding note on the affected ADR (ADR-002/008 for G1, ADR-005/008 for G2,
ADR-011 for G3) — accepted ADRs are immutable.

Suggested order: **G2 first** (it is design-blocking and the harder setup), **G3 in parallel** (independent, and its receiver seeds production ingestion config).
