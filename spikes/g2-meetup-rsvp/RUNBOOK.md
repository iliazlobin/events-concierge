# G2 Runbook — Meetup `createEventRsvp` spike

**Gate:** G2 (requirements §8) — DESIGN-BLOCKING for any autonomous-lane RSVP SLA. Feeds ADR-005 (pacer key + degrade threshold) and ADR-008 (Meetup detection point cost). Until this runs, no autonomous-lane SLA number may be declared (NFR-2(a) deferral, owner-decisions B14/B26).

## What it decides

| Question | Why it is load-bearing | Where the answer lands |
|---|---|---|
| **A. Auto-join open groups?** | If `createEventRsvp` auto-joins an open group for a non-member, the on-SLA autonomous surface widens beyond "groups the user is already in." | FR-5.2 scope, LaneRouter table (ADR-003) |
| **B. Quota per-token / per-app / per-IP?** | Per-app forces the pacer to one global bucket + degrade-to-handoff; per-token keeps per-tenant pacing. | ADR-005 pacer key + G2 config flip |
| **C. Retry-idempotent?** | Backstops the FR-5.3 read-before-mutate guard against double-RSVP on a lost ACK. | FR-5.3, NFR-8 |
| **D. Real per-chain point cost?** | The ~15 pts estimate is unpublished; detection poll sizing (ADR-008) and the 500 pt/60 s headroom depend on the measured value. | ADR-005, ADR-008 (B26) |

## What you must provide

1. **A Meetup account** you control, ideally two (for test B's per-app vs per-token distinction — two users under the **same OAuth client id**).
2. **A Meetup Pro / OAuth consumer** (client id + secret) and an **OAuth bearer token** per account. Meetup's RSVP mutation requires an authenticated user token; the API is the only ToS-sanctioned RSVP modality (browser RSVP is prohibited for Meetup — d10/d11).
3. **A test group + event:** for test A/D, a group the operator is **NOT** a member of, with an upcoming event id. For test C (idempotency, mutates), an event the operator **can** RSVP to.

> This probe runs against your own account through the sanctioned API — legitimate feasibility testing, not automation abuse. It is dry-run by default, small-N, honors `X-RateLimit-Reset`/`Retry-After`, and never rotates identity or bypasses a block.

## Setup

```bash
cd spikes/g2-meetup-rsvp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MEETUP_TOKEN=<operator OAuth bearer token>
export MEETUP_TOKEN_2=<second account token, same client id>   # optional, for test B
```

Obtaining a token: create an OAuth consumer at https://www.meetup.com/api/oauth/ (or the Pro console), run the authorization-code or JWT flow for your account, and export the resulting access token. Tokens are short-lived; refresh before a run.

## Procedure

1. **Confirm the schema first** (the Meetup GraphQL schema is versioned; do not trust the harness's guessed mutation blindly):
   ```bash
   ./harness.py introspect
   ```
   Read the printed `rsvp`-mutation fields and rate-limit exposure. If the mutation name, input type, or the `RsvpResponse` enum differ from `QUERIES` in `harness.py`, edit `QUERIES['rsvp_mutate']` / `QUERIES['rsvp_state']` to match, then continue.

2. **Test A — auto-join (dry-run, then commit):**
   ```bash
   ./harness.py test-a --group <urlname-you-are-not-in> --event <eventId>          # dry-run: confirms non-member
   ./harness.py test-a --group <urlname-you-are-not-in> --event <eventId> --commit  # fires the RSVP once
   ```
   Verdict is AUTO-JOIN CONFIRMED (RSVP succeeded for a non-member) or MEMBERSHIP-GATED (refused). If it auto-joined, withdraw afterward (test-c pattern / the app UI).

3. **Test B — quota scope (read-only, safe):**
   ```bash
   ./harness.py test-b --n 40
   ```
   With `MEETUP_TOKEN_2` set, the harness interleaves calls on the second token and prints whether TOKEN_1's `X-RateLimit-Remaining` moves. Dropped ⇒ **per-app** (shared budget). Unchanged ⇒ **per-token**. For **per-IP**, run test-b from two egress IPs (e.g. two hosts / a VPN toggle) with the same token and compare — document the result manually.

4. **Test C — idempotency (mutates; needs `--commit`):**
   ```bash
   ./harness.py test-c --event <eventId-you-can-rsvp-to> --commit
   ```
   Fires the identical RSVP twice and reads the resulting state. Verdict: IDEMPOTENT (same RSVP id / errors-on-second) or NOT (new id / going count +1).

5. **Test D — per-chain point cost:**
   ```bash
   ./harness.py test-d --group <urlname> --event <eventId>
   ```
   Sums the read-membership + read-rsvp-state costs; add the create-rsvp cost observed in the committed test-C run for the full chain. Compare against ~15 pts.

## Interpreting the results into the design

- **A = auto-join** → open the LaneRouter `open-instant-join` row (currently gated behind a default-off flag in ADR-003); widen FR-5.2's on-SLA definition.
- **B = per-app** → flip `quota_scope[meetup]=app` in ADR-005; the pacer becomes one global 500 pt/60 s bucket + DRR + degrade-to-handoff past the 5-min threshold; **no per-tenant autonomous-lane SLA is declarable** at scale.
- **B = per-token** → per-tenant pacing holds; an autonomous-lane SLA number can be set from the measured chain cost + realized request mix (G1).
- **C = not idempotent** → the FR-5.3 read-before-mutate guard is strictly load-bearing (already in the design); confirm the guard's read reflects a just-committed RSVP with no propagation lag.
- **D** → feeds the 500 pt/60 s headroom math in ADR-005 and the ~50k-distinct-watched-events poll budget in ADR-008; if the real cost is materially above ~15 pts, ADR-008's per-app worst case tightens and adaptive cadence / per-token sharding move from contingency to launch requirement.

Record the four verdicts + the JSON report in `PROJECT.md` and open a superseding ADR note for ADR-005/008 if B or D diverges from the carried assumptions.
