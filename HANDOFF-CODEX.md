# Events Concierge — build handoff (for Codex)

You are picking up an in-progress build. A runnable, tested **foundation** already exists; your job is to extend it, keeping the same clean architecture and the same green bar. Work in `/Users/iliazlobin/Claude/events-concierge`.

## 0. Read first (source of truth)
- `PROJECT.md` — the project SoT; read its **State** and **Next steps** and the latest phase-log entry (2026-07-15 build) in full.
- `design/system-design.md` (8-section design), `design/requirements.md` (v0.3 draft; FR/NFR/AC numbers are cited throughout the code), `decisions/README.md` + `decisions/adr-0*.md` (11 ADRs — the binding architecture decisions), `design/owner-decisions.md` (owner-ratified defaults).
- `README.md` — how to run.

## 1. What already exists (do not rebuild)
Clean **hexagonal / ports-and-adapters** Python 3.12 codebase (~3,600 LOC, 62 modules), `mypy --strict` + `ruff` clean, **32 tests green**. Layers under `src/events_concierge/`:
- `domain/` — pure entities + logic (calendar-id hash, lane router, conflict gate, dedup, lifecycle transitions). No I/O, no framework. **Do not add I/O here.**
- `ports/` — typed `Protocol` interfaces. **These are contracts; changing one means updating all implementers + composition.**
- `application/` — saga services (`parsing`, `discovery`, `feed`, `registration`).
- `adapters/` — `postgres/` (catalog with pgvector+tsvector RRF retrieval + dedup-on-ingest; RLS tenant repos with a guarded `fn_transition`), `crawl/` (free public-JSON-LD `SourcePort` + ACL), `ranking/` (deterministic embedding + cosine reranker), `policy/` (data-driven PDP + Redis/in-memory pacer), `mock/` (in-memory KMS/SES/Calendar/broker/email + a `ConfirmingSource`).
- `workflows/` — Temporal two-tier spine: parent `EventRequestWorkflow` (discover→rank→attempt loop) + child `RegistrationWorkflow`; activities are module-level and reference the container via `set_container()`.
- `api/` — FastAPI: `/v1/onboard`, `/v1/requests` (starts the workflow), `/v1/feed` (the paginated ranked scroll), `/v1/unrsvp`, `/v1/tasks/{token}/done`, `/healthz`.
- `composition.py` — the ONLY place that imports concrete adapters; wires the graph. `config.py`, `policies.py`.
- `migrations/` (Alembic, 3 migrations), `tests/{unit,integration}/`, `spikes/{g2-meetup-rsvp,g3-relay-acceptance}/` (ready-to-run empirical-gate harnesses).

Cloud is **mocked** (`EC_MOCK_CLOUD=true`): AWS KMS/SES/S3, Google Calendar, Anthropic, Browserbase are in-memory fakes behind ports. This is intentional for the foundation.

## 2. How to run and verify (the bar)
Requires `uv` and Docker (Colima is running). From the repo root:
```
make install            # uv sync (Python 3.12 venv)
make up                 # postgres(5433) redis(6380) temporal(7234) + ui(8234)
make migrate            # applies migrations AS THE OWNER role
make test               # unit tests (no services needed)
make test-integration   # RLS, slice, Temporal workflow, API (needs `make up`)
make slice              # end-to-end demo (both lanes)
make lint typecheck     # ruff + mypy --strict
```
**Definition of done for any change:** `make lint typecheck test test-integration` all green, and new behavior covered by a test.

## 3. LOAD-BEARING GOTCHAS (read before touching the DB)
1. **RLS requires a non-superuser role.** The bootstrap `ec` role is a Postgres superuser and **bypasses RLS even under FORCE**. The app connects as **`ec_app`** (non-superuser, created in migration `0002`). Migrations run as the owner via `EC_MIGRATION_URL`; the app uses `EC_DATABASE_URL` (ec_app). Never point the app at the `ec` role or tenant isolation silently breaks.
2. **RLS fails closed via NULLIF.** Policies use `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` (migration `0003`) because a custom GUC reverts to `''` (not NULL) after a transaction-local set, and `''::uuid` errors. Keep this pattern in any new tenant table.
3. **Tenant context is per-transaction.** `infra/db.session_scope(tenant_id)` sets the GUC with `set_config(..., true)`. Catalog (tenant-neutral) tables use `session_scope(None)`.
4. **Local ports are remapped** (5432/6379/7233 were taken): postgres **5433**, redis **6380**, temporal **7234**, ui **8234**.
5. **Deterministic ids are load-bearing.** Calendar id = `base32hex(sha256(tenant + canonical_event_id))`; workflow id = `{tenant}:{canonical_event_id}`; the one-active-lifecycle-row-per-(tenant,event) partial unique index enforces idempotency. Don't mint fresh workflow ids to re-read an existing lifecycle row.

## 4. Conventions (match the existing code)
- Every module starts with `from __future__ import annotations`; full type annotations; `mypy --strict` must pass.
- Dependencies point inward: `adapters` import domain+ports+infra only (never another adapter, never application/workflows/api). Only `composition.py` imports concrete adapters.
- Cite the FR/NFR/AC/ADR a piece of code implements in its docstring (see existing files for the style). No emoji.
- New external integrations go **behind a port with a mock**, swappable via `composition.py` — keep `EC_MOCK_CLOUD` working so the suite runs offline.
- Accept the owner-ratified defaults in `design/owner-decisions.md`; don't re-litigate settled ADRs.

## 5. Your next increments (ordered; the top ones need NO external credentials)
Do these one at a time, each landing green with tests. After each, update `PROJECT.md`'s phase log.

1. **Granularize the register saga + wire the pacer.** Today `application/registration.py` does the whole saga in one path and the `Pacer` port is built but unused. Split registration into distinct Temporal activities matching ADR-003's saga (`resolve_membership → policy_gate → register_or_rsvp → await_confirmation → dedupe_calendar → write_to_calendar`), mint idempotency keys once in workflow state and pass them down, add compensations, and acquire a `Pacer` lease (keyed `(source, credential)`) before each source call. Add crash/retry tests asserting exactly-once effects (NFR-8).
2. **Outbox relay + notifier worker.** The guarded `fn_transition` already writes `outbox` rows atomically (FR-8.9). Build a relay worker that claims outbox rows (`FOR UPDATE SKIP LOCKED`, already in `PostgresOutboxRepository`), delivers via `NotificationPort` (mock SES) with the dedup key, and marks delivered. Test at-least-once + dedup.
3. **Real ranking behind `RankerPort`.** Replace/augment the deterministic reranker with a real cross-encoder + a per-user feature re-score (LightGBM), and feed scroll/dwell as implicit signals (FR-4). Keep the deterministic embedding as the offline test double so the suite still runs without a model.
4. **Scaffold the real Meetup + Luma `SourcePort` adapters** (structure + fixtures now; live calls when creds land). Meetup: API `discover` + `createEventRsvp` with the **read-before-mutate** guard (FR-5.3) and the typed error taxonomy; Luma: browser best-effort with **detect-then-submit** (FR-5.5). Test against recorded/fixture responses. NOTE: the empirical G2 (Meetup quota/auto-join/idempotency) and G3 (relay acceptance) spikes in `spikes/` must be run by the owner (they need a Meetup Pro OAuth consumer and a relay domain) before these adapters can be trusted on the SLA — build them so a real client swaps in behind the port.
5. **Real Google Calendar adapter behind `CalendarPort`** (OAuth, `freeBusy`, idempotent upsert insert→409→patch, IANA tz, secondary calendar) — swappable with the mock; blocked on a Google OAuth client, so scaffold + contract-test against a fake.

Deferred until the owner signs off requirements v0.3: the D1–D8 product deltas (attendance loop, RSVP sniping, weekly digest, out-of-band verification, etc.). Do not build those yet.

## 6. Guardrails
- Don't weaken RLS, the deterministic-id invariants, the policy pre-mutate guard, or the "cloud stays mockable" property.
- Don't commit to git unless the owner asks (the workspace is currently uncommitted).
- If you need an owner decision (a new external account, a scope change, a settled-ADR reversal), stop and ask rather than guess.
