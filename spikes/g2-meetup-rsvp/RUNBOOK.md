# G2 Runbook — Meetup `createEventRsvp` spike

**Gate:** G2 (requirements §8) — DESIGN-BLOCKING for any autonomous-lane RSVP SLA. Feeds ADR-005 (pacer key + degrade threshold) and ADR-008 (Meetup detection point cost). Until this runs, no autonomous-lane SLA number may be declared (NFR-2(a) deferral, owner-decisions B14/B26).

## What it decides

| Question | Why it is load-bearing | Where the answer lands |
|---|---|---|
| **A. Auto-join open groups?** | If `createEventRsvp` auto-joins an open group for a non-member, the on-SLA autonomous surface widens beyond "groups the user is already in." | FR-5.2 scope, LaneRouter table (ADR-003) |
| **B. Quota per-token / per-app / per-IP?** | Per-app forces the pacer to one global bucket + degrade-to-handoff; per-token keeps per-tenant pacing. | ADR-005 pacer key + G2 config flip |
| **C. Retry-idempotent?** | Backstops the FR-5.3 read-before-mutate guard against double-RSVP on a lost ACK. | FR-5.3, NFR-8 |
| **D. Real per-chain point cost?** | The ~15-point estimate is unpublished; detection poll sizing depends on the live API's observed accounting and enforcement behavior. | ADR-005, ADR-008 (B26) |

## What you must provide

1. **A Meetup account** you control, ideally two (for test B's per-app vs per-token distinction — two users under the **same OAuth client id**).
2. **A Meetup Pro / OAuth consumer** (client id + secret) and an **OAuth bearer token** per account. Meetup's RSVP mutation requires an authenticated user token; the API is the only ToS-sanctioned RSVP modality (browser RSVP is prohibited for Meetup — d10/d11).
3. **A test group + event:** for test A/D, a group the operator is **NOT** a member of, with an upcoming event id. For test C (idempotency, mutates), an event the operator **can** RSVP to.

> This probe runs against your own account through the sanctioned API — legitimate feasibility testing, not automation abuse. It is dry-run by default, small-N, stops on HTTP 429 while preserving the provider recovery signal, and never rotates identity or bypasses a block. Meetup's current help and GraphQL guide do not provide a sufficiently consistent fixed quota contract, so historical 500-point/60-second figures remain assumptions until this field run and the commercial contract establish otherwise.

## Setup

```bash
cd spikes/g2-meetup-rsvp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MEETUP_TOKEN=<operator OAuth bearer token>
export MEETUP_TOKEN_2=<second account token, same client id>   # optional, for test B
export MEETUP_REPORT_SALT=<at-least-32-random-bytes>            # never written to reports
```

Obtaining a token: create an OAuth consumer at https://www.meetup.com/api/oauth/ (or the Pro console), run the authorization-code or JWT flow for your account, and export the resulting access token. Tokens are short-lived; refresh before a run.

## Procedure

1. **Confirm the schema first** (the Meetup GraphQL schema is versioned; do not trust the harness's guessed mutation blindly). The harness defaults to Meetup's post-February-2025 third-party endpoint, `https://api.meetup.com/gql-ext`, documented in the [official migration guide](https://www.meetup.com/graphql/guide/):
   ```bash
   ./harness.py introspect
   ```
   Read the printed `rsvp`-mutation fields and rate-limit exposure. If the mutation name, input type, or the `RsvpResponse` enum differ from `QUERIES` in `harness.py`, edit `QUERIES['rsvp_mutate']` / `QUERIES['rsvp_state']` to match, then continue.

2. **Test A — auto-join (dry-run, then commit):**
   ```bash
   ./harness.py test-a --group <urlname-you-are-not-in> --event <eventId>          # dry-run: confirms non-member
   ./harness.py test-a --group <urlname-you-are-not-in> --event <eventId> --commit  # fires the RSVP once
   ```
   The harness re-reads membership after the mutation. It can distinguish AUTO-JOIN CONFIRMED,
   NON-MEMBER RSVP CONFIRMED, and an inconclusive failure. It declares MEMBERSHIP-GATED only when
   an authoritative documented membership-required code has been added to
   `DOCUMENTED_MEMBERSHIP_REQUIRED_CODES` and the post-state remains non-member. If registration
   succeeded, withdraw afterward through ordinary Meetup controls.

3. **Test B — quota scope (read-only, safe):**
   ```bash
   ./harness.py test-b --n 40
   ```
   With `MEETUP_TOKEN_2` set, the harness calls the second token and then rechecks token 1. Compare only observations in the same reset window. A shared decrement supports **per-app**; independent counters support **per-token**; missing headers are **inconclusive**, not evidence of unlimited capacity. Establish any per-IP behavior only through an approved second test host—never by rotating egress to evade a throttle.

4. **Test C — idempotency (mutates; needs `--commit`):**
   ```bash
   ./harness.py test-c --event <eventId-you-can-rsvp-to> --commit
   ```
   Fires the identical RSVP twice and reads the resulting state. The harness declares idempotency
   only when both calls return the same RSVP identity and the aggregate state is compatible with
   one RSVP. A generic second-call error is inconclusive; only an authoritative documented
   already-RSVPed code may be added to `DOCUMENTED_ALREADY_RSVPED_CODES` and used with a compatible
   post-state.

5. **Test D — per-chain point cost:**
   ```bash
   ./harness.py test-d --group <urlname> --event <eventId>
   ```
   Sums the read-membership + read-rsvp-state costs; add the create-rsvp cost observed in the committed test-C run for the full chain. Compare against ~15 pts.

## Interpreting the results into the design

- **A = auto-join** → open the LaneRouter `open-instant-join` row (currently gated behind a default-off flag in ADR-003); widen FR-5.2's on-SLA definition.
- **B = per-app** → flip `quota_scope[meetup]=app` in ADR-005; the pacer becomes one global measured/contracted bucket + DRR + degrade-to-handoff past the 5-min threshold; **no per-tenant autonomous-lane SLA is declarable** at scale without adequate capacity evidence.
- **B = per-token** → per-tenant pacing holds; an autonomous-lane SLA number can be set from the measured chain cost + realized request mix (G1).
- **C = not idempotent** → the FR-5.3 read-before-mutate guard is strictly load-bearing (already in the design); confirm the guard's read reflects a just-committed RSVP with no propagation lag.
- **D** → feeds the measured or contracted headroom math in ADR-005 and the watched-event poll budget in ADR-008; if the real cost is materially above ~15 points—or the API exposes no stable accounting—tighten the worst case and keep autonomous Meetup disabled until conservative pacing is validated.

Record the four verdicts + the JSON report in the owning GitHub issue or PR and open a superseding ADR note for ADR-005/008 if B or D diverges from the carried assumptions.
