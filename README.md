# Events Concierge

Discover events across reviewed sources, browse Events, Map and Calendar, and open the provider's
registration page. Profiles and saved filters personalize the discovery experience.

The design of record lives in [`design/`](design/), [`decisions/`](decisions/), and
[`PROJECT.md`](PROJECT.md). This README covers building, testing, and deployment packaging.

## Status

The private discovery deployment runs on shared `platform-dev` in `iz27-platform-dev`.
The [private runbook](deploy/development.md) owns current acceptance, access and recovery evidence.
Production identity and release gates remain open.

The current milestone is a private discovery candidate: Events, Map and Calendar browsing, shared
filters, event details, provider registration links, profiles and saved filters. Select
`EC_RELEASE_PROFILE=discovery`; chat, automated RSVP, notifications, Calendar sync, purchases and
programmatic API keys are deferred. The broader Temporal product remains available for development
under the full profile. [Acceptance gates](docs/production-operations.md#first-release-acceptance)
and the [private deployment runbook](deploy/development.md) distinguish implemented features from
verified deployment. Cloud services are mocked by default, and live integrations are opt-in.
The repository contains validated Terraform/OpenTofu and Helm staging scaffolds plus a
partial built-in GCP runtime provider. That provider supplies native GCS claim-check storage,
reviewed public discovery, PostgreSQL audit/consent boundaries, and optional Google Calendar
assembly. The full product profile still lacks notification delivery, notification-secret
protection, a production credential vault, and production Calendar binding/access. The discovery
profile can construct a non-mock catalog runtime with real GCS/PostgreSQL/Redis and the configured
OIDC BFF; its deferred product ports raise on every operation, including external cleanup. Erasure
stays pending when provider cleanup cannot be verified. Runtime construction is not production
acceptance; deployment, identity, recovery and erasure evidence remain required.

Deliberate foundation choices:

- Discovery uses owner-reviewed anonymous public JSON, JSON-LD, RSS, ICS, and HTML adapters behind
  typed source ports. Catalog refresh is an explicit networked operation, never an intake side
  effect.
- Meetup has two deliberately separate paths: exact reviewed public city pages feed the shared
  catalog anonymously through root schema.org `Event` JSON-LD, while OAuth membership/RSVP data
  remains tenant-scoped and is never promoted into that catalog. See the
  [Meetup ingestion design](design/meetup-ingestion.md).
- Recommendations are exposed through a personalized, cursor-paginated feed.
- `EC_MOCK_CLOUD=true` selects local mock auth, cloud, and action adapters. It is suitable for
  development and CI, not public traffic; it does not turn an explicitly invoked public catalog
  refresh into an offline fixture run.
- `EC_MOCK_CLOUD=false` requires the complete built-in BFF configuration and
  `EC_RUNTIME_PROVIDER_FACTORY=package.module:callable`; there is no unsafe fallback to header
  authentication, no-op CSRF verification, local claim storage, or mock notifications. The
  included `events_concierge.deployment.gcp_runtime:build_runtime_ports` factory is an intentionally
  incomplete deployment slice, not a bypass around the missing production ports.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) with Python 3.12
- Docker with the Compose plugin
- Chromium for the committed browser gate (`make browser-install` installs the matching build)

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
| Consumer web app (when enabled) | `http://127.0.0.1:3001` |
| FastAPI / legacy static fallback | `http://127.0.0.1:8000` |
| API liveness | `http://127.0.0.1:8000/healthz` |
| API readiness | `http://127.0.0.1:8000/readyz` |
| PostgreSQL | `127.0.0.1:5433` |
| Redis | `127.0.0.1:6380` |
| Temporal gRPC | `127.0.0.1:7234` |
| Temporal UI | `http://127.0.0.1:8234` |

Copy [`.env.example`](.env.example) to `.env` to configure Compose and runtime settings. Compose
replaces host-facing `localhost` dependency URLs with service-DNS URLs inside containers. The
current host-side Make targets assume the documented default dependency ports (`5433`, `6380`, and
`7234`); if you change those ports, invoke the underlying command with matching `EC_*` URLs.

## Consumer web app

Build and start the complete local product, then open the same-origin app:

```bash
make stack
open http://127.0.0.1:3001
```

The primary consumer interface is the Next.js App Router application in [`web`](web) at port
`3001`. It proxies authenticated API requests to FastAPI inside the Compose network, so the browser
keeps a same-origin request boundary. Port `8000` remains the API and legacy static fallback; port
`8234` is Temporal's operator console.

The discovery surface contains Events, Map and Calendar; the full development profile also includes
Chat and the deferred request lifecycle. Events, Map, and
Calendar share one stable filter rail: Today/This week/Weekend/This month presets, one reviewed
source selector, additive city and named-area selections (currently Bay Area, Manhattan, and Los
Angeles area), free/paid/unlisted state, an optional exact-dollar maximum, text search, and one
custom date-range picker. Selected places are ORed together; the other dimensions narrow that
union. PostgreSQL applies every predicate before stable keyset pagination, so a full earlier page
cannot hide matching later dates. A maximum-price filter includes free events and paid USD events
whose known maximum is at or below the ceiling; it excludes unlisted, unknown, and non-USD prices.
The source selector labels its aggregate as a number of **sources** and each individual option as a
number of **events**; those values intentionally use different units. Choosing a source preserves
compatible place selections and removes place selections that cannot match that source, so a stale
place filter cannot make the newly chosen source appear empty.

Date windows have two deliberate modes. Omitting both bounds means “live catalog” and returns only
events that have not started. Supplying both timezone-aware bounds makes that end-exclusive range
authoritative, including a wholly past month; source facets and counts use the same range. Calendar
month navigation keeps the selected source, sends that month’s exact bounds, and starts pagination
from the beginning. Historical results are a view over each reviewed source’s **latest successful
current projection**, not an audit archive: an event that has rolled off the provider’s latest
projection is not recoverable through this browse API even when an older canonical row remains.
See [the catalog-history design note](design/catalog-browse-history.md).

Event cards expand in place for compact event facts, description, provider handoff, and calendar
link. The map gives most of the viewport to the canvas and keeps a scrollable list of events in the
current map bounds; each preview names its retained source/provider rather than inferring one from
the event URL. Organizer, host, speaker, and organization names are searchable/filterable facts.
An external entity-profile icon appears only when the response already contains a verified direct
HTTPS profile (LinkedIn `/in/...` for a person; LinkedIn `/company/...` or an explicit HTTPS site for
an organization). The renderer validates the URL shape but does not establish the identity
association; that verification belongs to the metadata producer. The UI never manufactures
LinkedIn search links, and the shared catalog does not ingest or display a named attendee roster.

In the default `EC_MOCK_CLOUD=true` mode, the welcome screen creates a local-only account from an
email address and keeps its opaque tenant reference in browser storage. Chat interprets common
topic, timing, price, provider, and city phrases, then reads through the same current-observation
catalog boundary as the three browse views.

For frontend-only development, use `make web-install`, run the API with `make api`, and then run
`make web-dev`. Production-shaped frontend checks use `make web-typecheck` and `make web-build`.
The production Next.js server resolves `EC_API_ORIGIN` at request time rather than baking a Compose
service name into the image. Its same-origin `/v1`, `/auth`, `/healthz`, and `/readyz` routes reject
unsafe paths and forwarding headers, cap request bodies at 64 KiB, and bound the complete body read;
the FastAPI service remains internal.

While the signed-in page is visible, the shell watches only recent requests that are still
`received`/`started` and lack a selected outcome. It rechecks Recent briefs at a jittered 30-second
base cadence, backs off exponentially toward five minutes after failures, and stops after the
request resolves, ages past six hours, the document is hidden, or the user signs out. If request
truth changes, Plans and To do refresh once; navigation and manual controls still refresh their
collections directly. One timer chain plus per-resource generations prevent overlapping polls and
stale responses, and a transient background failure preserves the last rendered durable truth.

Local header authentication and local onboarding are development conveniences, not public-facing
security. With `EC_MOCK_CLOUD=false`, onboarding is absent and the supported production composition
requires the repository-owned OIDC authorization-code BFF with `EC_OIDC_BFF_ENABLED=true`. It uses state, nonce, and
S256 PKCE; verifies the signed issuer/audience/tenant/subject claims; keeps opaque login and session
handles in TTL-bounded Redis records; and issues only Secure, Path=/, no-Domain `__Host-` cookies.
Every authenticated mutation requires an exact same-origin `Origin` plus the browser-readable CSRF
cookie in `X-EC-CSRF`, matched against the digest bound to that server-side session. Logout revokes
Redis authority before clearing browser cookies, and `/readyz` removes a BFF replica from service
when its Redis session control plane is unavailable. Configuration is fail closed and the signed
tenant/subject pair must already match a provisioned product account. A real IdP registration,
secret-manager value, edge/TLS deployment, account-provisioning path, and deployment browser canary
remain operator-owned evidence rather than repository defaults.

Settings also exposes a separate account-erasure Danger Zone. The browser and API both require the
exact phrase `DELETE MY ACCOUNT`; production additionally performs a `prompt=login`, `max_age=0`
OIDC step-up bound to the same live session, tenant, and subject. An accepted request immediately
fences ordinary account access and clears browser credentials, while the independently leased worker
continues cleanup. Enabled live provider, Calendar, notification, claim-check, withdrawal, and
Temporal-start paths share the same tenant advisory-lock authority as erasure begin; an admitted
effect is cancellation-safely drained before the fence commits, and a later effect is refused. The
UI says “underway,” not “complete”: real-adapter field proof, legacy Calendar disposition, Temporal
physical/archive deletion, backup, retention, and legal-hold evidence remain explicit production
gates.

A newly migrated database contains the reviewed source registry but no event rows. User requests
read the persisted catalog and never crawl as an HTTP side effect. The local/mock Compose app
profile starts an enqueue-only cadence scheduler: every 300 seconds it checks whether at least one
reviewed source is due and, only then, appends one durable `refresh_due` command for the separate
command worker. Command workers claim a 300-second lease and renew that exact command/token pair
every `min(60 seconds, lease / 3)` while source work is active. If renewal fails, the worker cancels
the in-flight operation and makes no terminal command mutation; another worker may safely reclaim
the command after the lease expires. Set `EC_CATALOG_INGESTION_SCHEDULER_ENABLED=false` before
`make stack` to retain a manual-only catalog. Check the selected release’s migration head with `uv run alembic heads`. For an
immediate local pass or source-specific diagnostic, run one of:

```bash
make catalog-cadence
make catalog-refresh SOURCE_KEY=luma-sf
make catalog-refresh SOURCE_KEY=luma-nyc
make catalog-refresh SOURCE_KEY=meetup-sf
make catalog-refresh SOURCE_KEY=meetup-nyc
```

These commands can make outbound requests to owner-reviewed public sources; `catalog-cadence`
evaluates one bounded pass of sources currently due. The local scheduler uses the same durable
queue, registry/review, policy, pacing, and refresh-lease gates—it does not fetch directly.
`make slice` is a synthetic end-to-end test in a disposable database and does not populate the
persistent local product catalog. The [Meetup public-catalog runbook](docs/meetup-ingestion-runbook.md)
documents the anonymous source contract, verification steps, and privacy containment boundary.

The complete local stack also exposes the separate ingestion control room at
`http://127.0.0.1:3001/admin` (with the FastAPI fallback at
`http://127.0.0.1:8000/admin`). It queues refresh work durably and shows policy, cadence, source,
run, and command state without broadening registry or policy authority. See the
[ingestion admin guide](docs/ingestion-admin.md).

Useful manual checks after startup:

1. Complete onboarding and save a few interests.
2. Preview a brief and confirm that no durable request appears.
3. Choose “Find and handle it,” then confirm that the saved brief appears under Recent briefs.
4. Leave the page visible and confirm that a selected outcome, Plans, and To do converge without a
   reload; complete handoffs only after using the event site.
5. Stop Temporal temporarily and verify readiness says requests are safely queued while PostgreSQL
   remains available.

For exact expected text, safe outage recovery, an evidence template, and a disposable-account
erasure check, follow the [manual UI acceptance test plan](docs/manual-test-plan.md).

## Containerized application

The `app` Compose profile builds the locked runtime image, runs migrations, and starts application
processes:

```bash
make api       # FastAPI smoke only; durable workers and Next.js remain stopped
make stack     # Next.js, API, Temporal worker, workers/scanners, and dependencies
make ps
make app-logs
```

The full stack includes the Next.js frontend, API, Temporal workflow/activity worker, request-start
worker, notifier outbox worker, account-erasure convergence worker, change-delivery worker,
handoff-expiry repair worker, lifecycle-invariant scanner, and local ingestion-command worker. They
share the same claim-check volume and Redis pacing state. Local Compose retains one compatibility
worker process by default; the production Helm profile instead runs distinct transactional and
catalog worker roles on separate task queues, with immutable Temporal Worker Deployment build
identities. Each configured worker role has explicit workflow-task and activity limits (eight of
each by default), the request-start worker drains at most five recovered starts every two seconds,
and the nightly invariant scanner spaces Temporal liveness reads at ten calls per second per
process. Override
`EC_TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOW_TASKS`,
`EC_TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES`, `EC_REQUEST_START_BATCH_SIZE`, and
`EC_REQUEST_START_POLL_SECONDS` only after measuring CPU, database-pool, and child-workflow
amplification together. Keep `EC_LIFECYCLE_INVARIANT_LIVENESS_CALLS_PER_SECOND` within its
validated 1-20 range so a large read-only inventory does not crowd out Temporal health or workflow
traffic.

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
make browser-install  # one-time browser binary install
make test-browser
make test-integration
make quality
make quality-load
make slice
make build
```

Each service-backed test target creates a randomized `ec_test_*` PostgreSQL database, migrates it,
and drops only that allowlisted database on exit. The end-to-end `make slice` smoke uses the same
runner, so its demo lifecycle rows are disposable too. Tests refuse the persistent `ec` runtime
database; this prevents a later request-start worker from executing durable test fixtures. The
dependency containers remain shared, while Temporal integration cases use isolated test
environments/queues. Connection-routing query options are rejected so they cannot override the
visibly isolated database path.

`make test-browser` starts the real FastAPI shell on an ephemeral loopback port and drives it with
Python Playwright. Its JSON APIs are fulfilled by deterministic in-browser fixtures, so the gate
needs neither Docker nor network access after Chromium is installed. It verifies local onboarding,
preview-versus-durable request semantics, selected-outcome rendering, plans, actionable handoffs,
settings, exact typed erasure confirmation, its accepted non-polling state and browser-session
teardown, accessibility and error states, security headers, reduced motion, and desktop/mobile
overflow. Set
`EC_E2E_BROWSER_CHANNEL=chrome` to use an already-installed Google Chrome locally; CI always
installs the Playwright-pinned Chromium build. This hermetic product gate does not impersonate a
deployed identity provider or prove purpose-bound reauthentication and the deployment's
session-bound CSRF policy; those remain production-canary requirements.

GitHub Actions runs locked lint, strict type-checking, unit tests, the service-backed integration
suite, bounded quality repetition coverage, the Chromium consumer gate, Compose validation, and a
non-root container build smoke test. Integration services and volumes are removed even when a job
fails.

## Build and deploy the image

The multi-stage Dockerfile installs only locked runtime dependencies, installs the package
non-editably, includes Alembic migrations, and runs as UID/GID `10001` by default:

```bash
docker build -t registry.example/events-concierge:VERSION .
docker run --rm --env-file deployment.env \
  registry.example/events-concierge:VERSION alembic upgrade head
docker run --rm --env-file deployment.env -p 127.0.0.1:8000:8000 \
  registry.example/events-concierge:VERSION
```

Use an immutable image digest in the deployment. Run `alembic upgrade head` as a pre-deploy job
with the migration-owner database URL, then run API and workers with the non-owner application
URL so row-level security is enforced. The migration bootstrap creates the fixed `ec_app` role
without a source-controlled production password; its password is supplied through
`EC_APP_ROLE_PASSWORD_FILE`. Existing installations rotate that password through the separate,
disabled-by-default `rotate-app-role-password` maintenance Job before rolling out the matching new
application DSN. Cloud SQL Auth Proxy mode and direct-TLS mode are validated separately, and every
process has an explicit bounded SQLAlchemy pool budget. The same image supports worker commands
such as:

```bash
python -m events_concierge.workflows.worker
python -m events_concierge.workers.request_starter
python -m events_concierge.workers.notifier
```

Full-profile production requirements also include:

- set `EC_MOCK_CLOUD=false` and select a complete `EC_RUNTIME_PROVIDER_FACTORY`; extend or replace
  the included partial GCP factory before admitting traffic;
- have that provider supply a `NotificationSecretProtector` backed by production KMS/envelope
  encryption; `KmsNotificationSecretProtector` is the SDK-injected implementation and the stable
  AES-GCM key used by local mocks is intentionally non-production. Its envelope records the
  immutable KMS key identity returned by `GenerateDataKey`, so alias rotation cannot strand old
  ciphertext, and uses a random per-envelope KMS context identifier rather than tenant identity in
  provider audit metadata;
- supply a real notification channel such as `SesNotificationAdapter` with a regional SES v2
  client, verified sender, mandatory configuration set, delivery-event routing, and an RLS-scoped
  tenant repository; the adapter emits only a SHA-256 delivery-correlation tag, never the dedup key;
- set `EC_PUBLIC_BASE_URL` to the canonical HTTPS origin used by handoff-completion links;
- inject database, Redis, provider, and Temporal credentials from a secret manager. Runtime and
  migration settings support strict `*_FILE` loading for the committed CSI mount contract; the
  current Helm scaffold still overprovisions a shared runtime-secret/IAM bundle across Python
  processes and must be narrowed before claiming least privilege;
- set `EC_TEMPORAL_TLS_ENABLED=true` and `EC_TEMPORAL_API_KEY` for Temporal Cloud (plus
  `EC_TEMPORAL_TLS_DOMAIN` when required);
- tune `EC_TEMPORAL_RPC_TIMEOUT_SECONDS` only within its validated 0.1–60 second range; the
  five-second default bounds eager connects, workflow starts, signals, and liveness reads while
  durable queues retain retries;
- keep Temporal workflow/activity concurrency within the measured per-replica CPU and database
  connection budget; the conservative defaults are eight of each, not the SDK's broad adaptive
  worker defaults;
- pace request-start recovery with a bounded batch and minimum cycle interval; a recovered parent
  can fan out into several registration children, so start throughput is not child throughput;
- keep `EC_REQUEST_BODY_TIMEOUT_SECONDS` within its validated 0.1–60 second range; the ten-second
  default bounds the complete decoded body read in addition to the 64 KiB request-size ceiling;
- terminate TLS at a trusted edge, enable HSTS, preserve the application's Host/Origin,
  forwarded-scheme, and security-header contract, and restrict operational endpoints; keep the
  built-in BFF as the sole browser identity authority;
- provide persistent, encrypted claim-check/object storage shared by every API and worker replica;
- run managed PostgreSQL/pgvector, Redis, and Temporal rather than the local Compose dependencies;
- use `/healthz` for process liveness and `/readyz` for readiness. Readiness fails when PostgreSQL
  is unavailable, also fails when an enabled OIDC BFF cannot reach its Redis session control plane,
  and reports a Temporal outage as durable degradation while request starts remain protected by the
  database outbox.

The deployment provider callable receives `Settings` and returns
`events_concierge.runtime.RuntimePorts`. Provider construction is validated at startup, so a
partial production graph does not serve traffic.

Reusable OIDC BFF/session, signed-token, GCS and S3-compatible object stores, Google Calendar,
Meetup GraphQL, SES v2, and KMS-envelope adapters live under `events_concierge.adapters`; deployment
code still owns issuer/client registration and secrets, the notification and credential-vault
bindings, production Calendar access, verified domains, delivery-event ingestion, and network
controls. SES has no send idempotency token, so its adapter does not resolve ADR-009's held
post-send-ack ownership decision. See the
[production operations runbook](docs/production-operations.md) for release order, recovery,
monitoring, incident controls, and the external launch gates that remain open.

## Architecture

Ports-and-adapters (hexagonal): dependencies point inward, and the domain has no I/O dependency.

```text
src/events_concierge/
  domain/        pure entities, value objects, enums, and policy logic
  ports/         typed protocols implemented at the system boundary
  application/   use cases and durable worker services
  adapters/      PostgreSQL, crawl, ranking, policy, provider, and local mock adapters
  workflows/     Temporal parent/child workflows and activities
  workers/       durable workers, repair loops, and invariant scans
  api/           same-origin consumer web app plus FastAPI intake/read/action contracts
  runtime.py     validated deployment-owned production provider graph
  composition.py dependency injection and fail-closed runtime selection
```

Every module traces to requirements and ADRs, including tenant RLS, two-tier orchestration,
pre-mutation policy enforcement, shared pacing, the durable outbox, calendar reconciliation, and
central lifecycle change detection.
