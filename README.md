# Events Concierge

A multi-tenant service that turns a natural-language request ("find me something Friday evening
after work and sign me up") into a confirmed, reconciled calendar entry: discover events across
sources, rank them to the user's taste, register where the source permits (otherwise issue a
one-tap handoff), write the confirmed event to the calendar, and reconcile later changes.

The design of record lives in [`design/`](design/), [`decisions/`](decisions/), and
[`PROJECT.md`](PROJECT.md). This README covers building, testing, and deployment packaging.

## Status

This is a foundation build with a working Temporal vertical slice. Cloud services are mocked by
default, and live integrations are opt-in. The production runtime fails closed unless the
deployment supplies an authenticated provider factory for auth, object storage, notifications,
credentials, calendar access, and source-specific mutation ports.

Deliberate foundation choices:

- Discovery begins with owner-approved public JSON-LD sources behind `SourcePort`.
- Recommendations are a personalized, cursor-paginated feed alongside the digest surface.
- `EC_MOCK_CLOUD=true` selects local-only adapters. It is suitable for development and CI, not
  public traffic.
- `EC_MOCK_CLOUD=false` requires
  `EC_RUNTIME_PROVIDER_FACTORY=package.module:callable`; there is no unsafe fallback to header
  authentication, local claim storage, or mock notifications.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) with Python 3.12
- Docker with the Compose plugin

Install the locked application and development dependencies:

```bash
make install
```

## Local development

Start dependency services, apply migrations, and run the tests:

```bash
make up
make migrate
make test
make test-integration
make quality-load
```

`make up` starts only PostgreSQL/pgvector, Redis, Temporal, and the Temporal UI. It does not start
application code. The default Compose profile is intentionally dependency-only.

Local endpoints are bound to loopback:

| Service | Address |
| --- | --- |
| API (when enabled) | `http://127.0.0.1:8000` |
| API liveness | `http://127.0.0.1:8000/healthz` |
| API readiness | `http://127.0.0.1:8000/readyz` |
| PostgreSQL | `127.0.0.1:5433` |
| Redis | `127.0.0.1:6380` |
| Temporal gRPC | `127.0.0.1:7234` |
| Temporal UI | `http://127.0.0.1:8234` |

Copy [`.env.example`](.env.example) to `.env` to customize ports and runtime settings. Compose
replaces host-facing `localhost` dependency URLs with service-DNS URLs inside containers.

## Containerized application

The `app` Compose profile builds the locked runtime image, runs migrations, and starts application
processes:

```bash
make api       # API plus dependencies; background workers remain stopped
make stack     # API, Temporal worker, durable relays/scanners, and dependencies
make ps
make app-logs
```

The full stack includes the API, Temporal workflow/activity worker, request-start relay, notifier
outbox relay, change-delivery worker, handoff-expiry repair worker, and lifecycle-invariant
scanner. They share the same claim-check volume and Redis pacing state.

Ordinary shutdown preserves PostgreSQL, Temporal, and claim-check volumes:

```bash
make down
```

Deleting local data is a separate, guarded operation:

```bash
make reset CONFIRM=reset
```

The Compose stack uses fixed development credentials and Temporal's auto-setup image. It is for
local validation, not production infrastructure.

## Tests and CI

Useful targets:

```bash
make lint
make typecheck
make test-unit
make test-integration
make quality
make quality-load
```

GitHub Actions runs locked lint, strict type-checking, unit tests, the service-backed integration
suite, bounded quality repetition coverage, Compose validation, and a non-root container build
smoke test. Integration services and volumes are removed even when a job fails.

## Build and deploy the image

The multi-stage Dockerfile installs only locked runtime dependencies, installs the package
non-editably, includes Alembic migrations, and runs as UID/GID `10001` by default:

```bash
docker build -t registry.example/events-concierge:VERSION .
docker run --rm --env-file deployment.env \
  registry.example/events-concierge:VERSION alembic upgrade head
docker run --rm --env-file deployment.env -p 8000:8000 \
  registry.example/events-concierge:VERSION
```

Use an immutable image digest in the deployment. Run `alembic upgrade head` as a pre-deploy job
with the migration-owner database URL, then run API and workers with the non-owner application
URL so row-level security is enforced. The same image supports worker commands such as:

```bash
python -m events_concierge.workflows.worker
python -m events_concierge.workers.request_starter
python -m events_concierge.workers.notifier
```

A real deployment must also:

- provide the deployment-owned runtime provider package in the image and set
  `EC_MOCK_CLOUD=false` plus `EC_RUNTIME_PROVIDER_FACTORY`;
- have that provider supply a `NotificationSecretProtector` backed by production KMS/envelope
  encryption; the stable AES-GCM key used by local mocks is intentionally non-production;
- set `EC_PUBLIC_BASE_URL` to the public HTTPS origin used by handoff-completion links;
- inject database, Redis, provider, and Temporal credentials from a secret manager;
- set `EC_TEMPORAL_TLS_ENABLED=true` and `EC_TEMPORAL_API_KEY` for Temporal Cloud (plus
  `EC_TEMPORAL_TLS_DOMAIN` when required);
- tune `EC_TEMPORAL_RPC_TIMEOUT_SECONDS` only within its validated 0.1–60 second range; the
  five-second default bounds eager connects, workflow starts, signals, and liveness reads while
  durable queues retain retries;
- keep `EC_REQUEST_BODY_TIMEOUT_SECONDS` within its validated 0.1–60 second range; the ten-second
  default bounds the complete decoded body read in addition to the 64 KiB request-size ceiling;
- terminate TLS and enforce signed edge authentication before the API;
- provide persistent, encrypted claim-check/object storage shared by every API and worker replica;
- run managed PostgreSQL/pgvector, Redis, and Temporal rather than the local Compose dependencies;
- use `/healthz` for process liveness and `/readyz` for readiness. Readiness fails when PostgreSQL
  is unavailable and reports a Temporal outage as durable degradation while request starts remain
  protected by the database outbox.

The deployment provider callable receives `Settings` and returns
`events_concierge.runtime.RuntimePorts`. Provider construction is validated at startup, so a
partial production graph does not serve traffic.

Reusable signed-OIDC and S3-compatible object-store adapters live under
`events_concierge.adapters`; deployment code still owns issuer/client policy, SDK client and bucket
construction, credentials, and network controls. See the
[production operations runbook](docs/production-operations.md) for release order, recovery,
monitoring, incident controls, and the external launch gates that remain open.

## Architecture

Ports-and-adapters (hexagonal): dependencies point inward, and the domain has no I/O dependency.

```text
src/events_concierge/
  domain/        pure entities, value objects, enums, and policy logic
  ports/         typed protocols implemented at the system boundary
  application/   use cases and durable relay services
  adapters/      PostgreSQL, crawl, ranking, policy, provider, and local mock adapters
  workflows/     Temporal parent/child workflows and activities
  workers/       durable relays, repair loops, and invariant scans
  api/           FastAPI intake, feed, withdrawal, and handoff contracts
  runtime.py     validated deployment-owned production provider graph
  composition.py dependency injection and fail-closed runtime selection
```

Every module traces to requirements and ADRs, including tenant RLS, two-tier orchestration,
pre-mutation policy enforcement, shared pacing, the durable outbox, calendar reconciliation, and
central lifecycle change detection.
