# Events Concierge

A multi-tenant service that turns a natural-language request ("find me something Friday evening after work and sign me up") into a confirmed, reconciled calendar entry: **discover** events across sources, **rank** them to the user's taste, **register** where the source permits (else a one-tap handoff), **write** the confirmed event to the calendar, and **reconcile** it when organizers change or the user withdraws.

Design of record lives in [`design/`](design/), [`decisions/`](decisions/) (ADRs), and [`PROJECT.md`](PROJECT.md). This README covers the **build**.

## Status

Foundation build. Clean hexagonal architecture, runs locally against docker-compose dependencies with cloud services (AWS/Google/Anthropic/Browserbase) **mocked** behind ports. The first vertical slice — intake -> discover -> rank -> policy -> register/handoff -> calendar -> lifecycle — is wired through Temporal.

Deliberate foundation choices (owner-directed, 2026-07-15):
- **Discovery starts free-crawl** (public-page JSON-LD) behind `SourcePort`; API connectors (Ticketmaster/Meetup) are a drop-in adapter swap later.
- **Recommendations are a personalized ranked feed** (cursor-paginated for infinite scroll), alongside the 1-3-pick digest surface.
- **Cloud is mocked** (`EC_MOCK_CLOUD=true`) by default. An explicit Cohere key can select the existing
  cross-encoder, and an explicit Google Calendar flag plus a provisioned tenant-scoped access port can select the
  existing Calendar adapter. Google deployment code may expose that port through the trusted
  `EC_GOOGLE_CALENDAR_ACCESS_FACTORY=module:callable` bootstrap hook; OAuth/token provisioning, real
  KMS-envelope storage, and SES remain owner-gated.

## Architecture

Ports-and-adapters (hexagonal). Dependencies point inward; the domain knows nothing about I/O.

```
src/events_concierge/
  domain/        pure entities, value objects, enums, and logic (calendar-id hash,
                 lane router, dedup key, conflict gate) -- no I/O, no framework
  ports/         typed Protocol interfaces the outside world must satisfy
  application/   use cases / services that orchestrate ports (the saga steps)
  adapters/      concrete implementations of ports
    postgres/    catalog + tenant-RLS repositories (pgvector + tsvector)
    crawl/       free public-page JSON-LD SourcePort + ACL normalizer + dedup
    ranking/     RRF fuse + rerank + re-score -> paginated feed
    policy/      declarative policy engine + pre-mutate guard + Redis pacer
    mock/        in-memory fakes for KMS/SES/S3, Google Calendar, Anthropic, Browserbase
  workflows/     Temporal EventRequest (parent) + Registration (child) workflows + activities
  api/           FastAPI intake + inbound contracts (request, un-RSVP, mark-done, feed)
  config.py      pydantic-settings
  composition.py DI wiring (the composition root)
```

## Run it

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make install          # create the 3.12 venv, install deps
make up                # start postgres (pgvector), redis, temporal
make migrate           # apply the schema
make test              # unit tests (no services needed)
make test-integration  # integration tests against compose services
make slice             # run the end-to-end vertical slice against mocks
```

Temporal UI at http://localhost:8233 once `make up` is healthy.

## Mapping to the design

Every module traces to the requirements (`design/requirements.md`) and ADRs (`decisions/`):
- domain calendar-id + dedup -> FR-9.2 / FR-3.8, ADR-001
- lane router + policy pre-mutate guard -> FR-5.1 / FR-5.9, ADR-003 / ADR-004
- two-tier workflows -> FR-8.1 / FR-5.0, ADR-003
- crawl SourcePort + ACL + dedup -> FR-3.x, ADR-001
- ranked feed -> FR-4.x (personalized, paginated)
- policy pacer -> FR-10.4, ADR-005
