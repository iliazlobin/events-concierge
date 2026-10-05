# Events Concierge

Current product scope and delivery priorities. GitHub Issues and PRs own task progress and review evidence.

- [Architecture](ARCHITECTURE.md): components, code map and invariants.
- [README](README.md): development commands and workflow.
- [Deployment and recovery](deploy/development.md): serving revisions, configuration, access and recovery evidence.

## Current milestone: private discovery candidate

| Status | Scope |
| --- | --- |
| Included | Search, shared filters, Events/Map/Calendar, read-only entity graphs, event details, provider registration links, profiles and saved filters. |
| Deferred | Chat, automated RSVP, managed handoffs, notifications, Calendar synchronization, purchases and programmatic API keys. |
| Implemented; activation pending | Guest catalog access, Google/Apple accounts with legal acceptance, datastore TLS and self-hosted Temporal mTLS. |
| Accepted development behavior | Private discovery and recovery using mock external product adapters. |
| Production acceptance pending | OAuth configuration, private HTTPS and deployed identity/transport verification. |

- `EC_RELEASE_PROFILE=discovery` selects product scope; retained full-profile code does not expand it.
- `EC_MOCK_CLOUD` controls integration behavior independently of product scope.
- [Consumer accounts](deployment/consumer-identity.md): managed signup, reauthentication/erasure and admin restricted to `iliazlobin91@gmail.com`. The legacy Google-only pilot retains its deletion limitation.
- [Release acceptance](docs/production-operations.md#first-release-acceptance) owns launch gates. Development acceptance does not establish production acceptance.

## System design

| Component | Responsibility |
| --- | --- |
| Next.js | Web application and same-origin proxy. |
| FastAPI | API contracts. |
| PostgreSQL | Application truth and published catalog. |
| Redis | Shared pacing and configured sessions. |
| Temporal and Python workers | Registered workflow/activity execution. |
| Shared GCP foundation | Networking and GKE. |
| Application repository | Releases, identities, data and recovery procedures. |

- Authenticate and provision users; isolate tenant data and credentials.
- Keep consumer and operator authority separate; public catalog access grants neither tenant nor operational authority.
- Consumer searches read published PostgreSQL projections; they never initiate scraping.
- Ingestion admits reviewed sources, applies shared pacing and bounded work, and publishes behind lease fences.
- Failed runs preserve the last successful projection.
- Historical browsing uses retained current observations; it is not a complete archive. [Catalog history](design/catalog-browse-history.md).
- Preserve provider identity, visibility and event semantics. [Event semantics](design/catalog-event-semantics.md).
- Display-name matches do not establish verified identities; attendee rosters stay excluded. [Entity catalog](design/entity-catalog-and-research.md).
- Source admission, expanded crawl budgets and external enrichment require review. [Enrichment controls](design/entity-enrichment-control-plane.md).
- Database authority remains necessary for retries and remote effects, including Temporal activities.
- Source integration, image publication, migration and deployment are separate operations.
- Deferred registration, lifecycle and provider requirements remain in [requirements](design/requirements.md) and [ADRs](decisions/README.md).
- The no-Gmail boundary is permanent. [ADR-011](decisions/adr-011-relay-inbox-no-gmail.md).

## Delivery priorities

1. Reconcile unpublished web/admin/backend work through scoped PRs into `main`; preserve existing worktrees and uncommitted changes.
2. Validate candidate web, backend and browser behavior using [AGENTS.md](AGENTS.md).
3. Complete authenticated private Helm composition: existing in-cluster stores, per-process permissions and certificate/secret mounts.
4. Configure Google/Apple Identity Platform, approved legal pages and trusted HTTPS. [Remaining release work](deploy/development.md#remaining-release-work).
5. Rehearse the combined candidate, migration and transport rollback before an authorized release.
6. Verify authentication/CSRF, tenant isolation, discovery, collection, worker recovery, monitoring and backups in the target environment.

[Production operations](docs/production-operations.md) owns acceptance. Passing CI alone does not close release gates.

## Conventions

- Approved source: [iliazlobin/events-concierge](https://github.com/iliazlobin/events-concierge). Permanent integration branch: `main`.
- Use isolated task worktrees; preserve unrelated local changes. Follow the [development workflow](README.md#development-workflow).
- The `legacy-prototype` remote is reference material, not the release repository.
- Use job names: request-start worker, notification worker, change-delivery worker, watch-projection worker and ingestion-command worker.
- Use **background workers** for the group. Preserve proper provider names such as RelayInbox.
- Publish reviewed source before citing it in living documentation.
- Use commit-pinned `blob/<full-commit-sha>/<path>` or `tree/<full-commit-sha>/<path>` GitHub links.
- Use concise repository-relative labels and verified line anchors; verify every target at the published revision.
- Label unpublished references pending. Distinguish source/test evidence from deployed behavior.
- Keep scope here, implementation detail beside code/design, operational commands in runbooks, and task evidence in Issues/PRs.
- [AGENTS.md](AGENTS.md) owns agent boundaries; [WORKFLOW.md](WORKFLOW.md) owns Symphony assignments. Neither grants deployment or provider-mutation authority.
