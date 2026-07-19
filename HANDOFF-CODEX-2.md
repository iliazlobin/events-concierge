# Events Concierge — follow-up for Codex (round 2)

Your five increments landed well: an adversarial review found **no correctness bug on the happy path and no invariant regression**, and the saga's exactly-once behavior is genuinely proven by crash/replay tests. The work below is a tight fixup pass — two real edge-case bugs, one gap against the owner's original ask, wiring so the real adapters are reachable, and a set of **missing negative/safety tests** (the code is correct on inspection but a future regression would pass the suite silently). Same repo (`/Users/iliazlobin/Claude/events-concierge`), same conventions and bar as before: read `HANDOFF-CODEX.md` for the invariants; every change must keep `make lint typecheck test test-integration` green and be covered by a test.

## A. Correctness fixes (do first)
1. **Google 403 conflation** — `src/events_concierge/adapters/google_calendar/calendar.py` `_raise_for_error` maps *every* 401/403 to `GoogleCalendarReconsentRequiredError`. Google returns 403 for transient `rateLimitExceeded`/`userRateLimitExceeded`/`quotaExceeded` too. Inspect the Google error `reason`/`status`: only `invalid_grant` / insufficient-permission / auth failures raise the typed re-consent error (FR-9.7); rate/quota 403 must raise a distinct **retryable** error. Add tests for both a re-consent 403 and a rate-limit 403.
2. **Outbox retry-budget exhaustion under contention** — `src/events_concierge/application/outbox.py` / `adapters/postgres/tenant_repos.py`: `attempt_count` is incremented on every claim, including `BUSY` (a competing relay holds the ledger lease) and lost-lease reschedules. Repeated transient contention can hit the 5-attempt terminal-fail budget for a message that never actually failed delivery. Separate **transient contention** (BUSY / lease-lost → reschedule without consuming a delivery attempt) from **delivery failure** (send raised → consume an attempt). Add a test that BUSY reschedules do not advance toward terminal failure.

## B. Build the owner's scroll/dwell feedback loop (the M3 gap)
The owner asked for "ranked recommendations that adjust to user preferences, as a scroll." The `PersonalizedRanker` plumbs `implicit_affinities` (weighted ~0.05) but **nothing ever writes them** — the scroll does not learn from behavior. Close the loop:
- Add an inbound signal contract (extend the API and a port) to record per-user **scroll/dwell/click/dismiss** signals against a `canonical_event_id` (or its taste features), stored under RLS.
- Feed those stored implicit affinities into `PersonalizedRanker` so a user who dwells on/*opens* jazz events gets jazz ranked higher on the next feed, and dismissals demote.
- Prove it: a test where recording implicit signals for a user **measurably reorders** their subsequent feed (distinct from the existing explicit-affinity test).

## C. Make the real adapters reachable (currently dead code)
Neither the Cohere cross-encoder nor the Google Calendar adapter is selectable — there is no settings key and no non-mock branch, so `composition.build_container` always uses the deterministic/mock doubles.
- Add settings (e.g. `EC_COHERE_API_KEY`, `EC_GOOGLE_CALENDAR_ENABLED`/binding source) and a composition branch that injects `CohereRerankCrossEncoder` and the Google `CalendarPort` **when configured**, still defaulting to the deterministic/mock graph so the offline suite runs with zero creds. Add a composition test asserting: default → mocks; configured → real adapters injected (with a fake transport).

## D. Close the untested safety/branch gaps (correct on inspection, would regress silently)
Add tests — no production-code change expected unless a test surfaces a bug:
1. **Outbox durable ledger dedup (highest priority in D):** the DELIVERED short-circuit and BUSY branch (`outbox.py:76-85`) are the whole point of `notification_ledger`, but the only redelivery test converges via `MockNotifier`'s in-memory dedup, not the ledger. Add a relay test with the outbox fake returning `claim=DELIVERED` (assert the port is NOT called again, outbox acked) and one returning `BUSY` (assert reschedule, no send); and an integration test that reopens a delivered row (`delivered_at=NULL`, clear the lease) and asserts the **ledger** prevents a second send.
2. **Saga data-plane guard:** deny `policy.evaluate()` between `policy_gate` and the mutation → assert `adapter.register` is never called and the outcome is `NEEDS_HANDOFF`.
3. **Fail-closed membership:** `membership_resolver → UNKNOWN` (and one that raises) → assert the resolved lane plan excludes `AUTONOMOUS_SLA` and the event routes to handoff with no `register()` call.
4. **Confirmation out-of-band verify:** signal fires but the fresh source read is still `PENDING`/`NOT_PRESENT` → assert the child stays `AWAITING_CONFIRMATION` (no `lifecycle.registered` outbox row) until a later read confirms.
5. **Meetup:** read-before-mutate no-op — a retry after an already-`CONFIRMED` read performs no second mutation (AC-36); `Modality.API` to Luma and `Modality.BROWSER` to Meetup are structurally refused (FR-3.2); `NOT_PRESENT` + positive price → `PAYWALL` abort-without-submit (FR-5.10/AC-41).
6. **Google:** happy-path insert (200, no PATCH) proving "patch only on conflict"; an IANA-reject negative test (a bare offset raises, FR-9.5).
7. **Cohere:** the defensive `_parse_scores` branches (non-dict payload, missing results, bad index/score, duplicate index).

## E. Optional nit
`write_to_calendar` couples the calendar upsert and the `REGISTERED→SCHEDULED` transition in one activity; if only the transition fails all retries, the forward-recovery emits a `calendar_recovery_required` task even though the entry WAS written (misleading operator signal, still ADR-007-safe). Either split the transition into its own retried step or have the compensation distinguish upsert-failure from transition-failure.

Do A→B→C→D in order; land each green with its test and update `PROJECT.md`. If any of these turns out to need a settled-ADR change or an owner decision, stop and ask rather than guess.
