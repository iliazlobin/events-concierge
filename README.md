# Events Concierge

Discover events, browse Events/Map/Calendar and entity graphs, and open the provider's registration page.

- [Architecture](ARCHITECTURE.md): code map, runtime flows and invariants.
- [Contributor guidance](AGENTS.md): task boundaries and checks.
- [Current milestone](PROJECT.md#current-milestone-private-discovery-candidate): approved product scope.
- [Runtime contracts](design/system-design.md): implementation constraints; [Notion design](https://app.notion.com/p/391d865005a88164a182eabc18fe068f): architecture and rationale.

## Status

- **Current milestone:** private discovery, profiles and saved filters; `EC_RELEASE_PROFILE=discovery`.
- **Deferred:** chat, automated RSVP, notifications, calendar sync, purchases and API keys.
- **Private GCP deployment:** shared `platform-dev`; [deployment runbook](deploy/development.md) owns current state.
- **Release gates:** production identity, recovery and deployed acceptance remain open.
- **Local defaults:** full development profile; `EC_MOCK_CLOUD=true`. Mock identity is not for public traffic.
- **Public-source refreshes can make real network requests even in mock mode.**
- **Non-mock runtime:** requires the configured OIDC BFF and a validated provider factory; missing ports fail closed.
  The built-in GCP factory supports the discovery slice; full-profile production bindings remain incomplete.

See [release acceptance](docs/production-operations.md#first-release-acceptance) before treating a build as deployable.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.12.
- Docker with Compose.
- Node 22 for frontend development.
- Chromium for browser checks: `make browser-install`.

```bash
make install
test -e .env || cp .env.example .env
```

Keep existing `.env` settings and credentials private. [`.env.example`](.env.example) documents local configuration.

## Development workflow

1. Start from freshly fetched `origin/main` in an isolated worktree or Symphony clone.
2. Use a short-lived `codex/<issue>-<description>` branch; omit the issue number when absent.
3. Define outcome and acceptance in the Symphony task; GitHub retains issue content and PR evidence. Read [AGENTS.md](AGENTS.md) and the affected component.
4. Open a ready PR against `main`; use draft only when requested. CI and independent review must cover the current head.
5. Resolve conflicts and rerun affected checks/review after changes. Squash-merge only when authorized.
6. Verify integration into `main` before deleting the task branch. Preserve active worktrees, dependent PRs and unpublished work.
7. Deploy only an approved revision with immutable image digests. Deployment, migrations, infrastructure and access changes need separate authorization.

- `main` is the permanent integration branch. Preserve unrelated dirty work.
- Stack PRs only for a concrete dependency. Name the parent and final target; retarget to `main` after the parent merges.
- Reconcile squashed parent commits and repeat checks/review. A closed PR is not proof of integration.
- [Release acceptance](docs/production-operations.md#first-release-acceptance) and [deployment runbook](deploy/development.md) own release operations. CI does not deploy.
- **Symphony:** [WORKFLOW.md](WORKFLOW.md) defines assignments and handoffs. Workers cannot publish or merge.
- The host pins a reviewed `base_sha` on `main`; scheduler and publisher must advance together.
  Preserve holds, budgets and evidence; follow the [host profile procedure](https://github.com/iliazlobin/symphony/blob/main/profiles/events-concierge/README.md#change-the-baseline).
- Automatic merge stays disabled until branch protection, required checks and the explicit low-risk allowlist are verified.

## Local development

For the owner-operated local environment:

```bash
make up       # Dependencies only: PostgreSQL/pgvector, Redis, Temporal and Temporal UI
make migrate  # Apply migrations to the local application database
make test
```

- Compose defaults share a project name, ports and volumes. A worktree does not isolate runtime state.
- Assigned agents follow [AGENTS.md](AGENTS.md); do not start or migrate the owner's stack as a generic check.
- Host Make targets assume the ports below. Custom ports require matching `EC_*` URLs in the underlying commands.
- Compose uses service-DNS dependency URLs inside containers.

| Service | Loopback address |
| --- | --- |
| Consumer app | `http://127.0.0.1:3001` |
| Ingestion admin (when enabled) | `http://127.0.0.1:3001/admin` |
| FastAPI / legacy static fallback | `http://127.0.0.1:8000` |
| Liveness / readiness / release identity | `http://127.0.0.1:8000/healthz`, `/readyz`, `/versionz` |
| PostgreSQL | `127.0.0.1:5433` |
| Redis | `127.0.0.1:6380` |
| Temporal gRPC | `127.0.0.1:7234` |
| Temporal UI | `http://127.0.0.1:8234` |

## Consumer web app

Start the complete local product:

```bash
make stack
open http://127.0.0.1:3001
```

Frontend development with the containerized API (includes local migrations):

```bash
make web-install
make api
make web-dev
```

- Next.js in [`web/`](web/) proxies same-origin API requests to FastAPI.
- Discovery reads the published catalog; browsing never starts provider scraping.
- Local onboarding uses mock identity. Non-mock mode requires OIDC, secure sessions and CSRF checks.
- A newly migrated database has a source registry but no events.
- Compose enables the 300-second enqueue-only cadence by default when unset; `.env.example` disables it.
  Set `EC_CATALOG_INGESTION_SCHEDULER_ENABLED=false` for manual-only collection.
- Catalog commands can fetch reviewed public sources. Registry, policy, pacing and lease gates still apply:

```bash
make catalog-cadence
make catalog-refresh SOURCE_KEY=luma-sf
```

- Local admin requires `EC_ADMIN_INGESTION_ENABLED=true` and a loopback bind.
- [Ingestion admin](docs/ingestion-admin.md): operator controls, source policy and diagnostics.
- [Catalog semantics](design/catalog-event-semantics.md), [browse history](design/catalog-browse-history.md) and [entities](design/entity-catalog-and-research.md): facts, filters, dates and read-only graphs.
- [Meetup ingestion](docs/meetup-ingestion-runbook.md): anonymous catalog collection; OAuth/RSVP data remains tenant-scoped.
- [Manual acceptance](docs/manual-test-plan.md): discovery checks, identity boundaries and erasure acceptance.
- [Account settings](design/account-settings-vertical.md): erasure fencing and cleanup. Accepted erasure is not completed erasure.

## Containerized application

| Command | Effect |
| --- | --- |
| `make api` | Build/start API and dependencies; run local migrations; omit Next.js and durable workers |
| `make stack` | Build/start Next.js, API, workers, dependencies and migrations |
| `make ps` | Show service state |
| `make app-logs` | Follow application and worker logs |
| `make down` | Stop services; preserve data volumes |
| `make reset CONFIRM=reset` | Delete local data volumes; destructive |

- Compose uses fixed development credentials and Temporal auto-setup. It is local infrastructure.
- APIs and workers share claim-check storage and Redis pacing.
- [Process inventory](docs/production-operations.md#required-process-inventory) owns worker roles, queues and deployment limits.

## Tests and CI

| Command | Coverage |
| --- | --- |
| `make lint`, `make typecheck` | Python lint and strict types |
| `make test-unit` | Unit tests without external services |
| `make web-typecheck`, `make web-build` | Frontend types and production bundle |
| `make browser-install`, `make test-browser` | Install Chromium; run fixture-backed consumer browser gate |
| `make test-integration` | Service-backed integration tests |
| `make quality`, `make quality-load` | Synthetic workflow matrix and bounded repetitions |
| `make slice` | Synthetic end-to-end test; does not populate the product catalog |
| `make build` | Build application images |

- Frontend behavior scripts live in [`web/package.json`](web/package.json).
- Service-backed tests create, migrate and remove randomized `ec_test_*` databases; the retained `ec` database is refused.
- Dependency services remain shared. Temporal tests use isolated environments/queues.
- Browser fixtures do not prove deployed identity, CSRF, provider access or erasure completion.
- [CI](.github/workflows/ci.yml) and [deployment validation](.github/workflows/deployment-validation.yml) define required jobs.

## Build and deploy the image

- [Dockerfile](Dockerfile): locked runtime dependencies, migrations and non-root UID/GID `10001`.
- Use immutable image digests. Match source, checks, review and deployed revision.
- Run migrations with the owner role; API and workers use non-owner roles with RLS.
- Inject credentials from the deployment secret manager; never commit populated environment files.
- Enforce production OIDC/CSRF, TLS, scoped credentials, bounded pools and shared durable storage.
- A working provider factory or healthy endpoint does not prove release acceptance.
- [Production operations](docs/production-operations.md): release gates, process limits, secret rotation and incidents.
- [Private deployment and recovery](deploy/development.md): application release, access, backup and restore.
