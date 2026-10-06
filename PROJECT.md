# Events Concierge

Approved product scope. Symphony owns task stages and acceptance; GitHub owns issue content, code and review evidence.

- [Architecture](ARCHITECTURE.md): components, code map and invariants.
- [README](README.md): development commands and workflow.
- [Deployment and recovery](deploy/development.md): serving revisions, configuration, access and recovery evidence.

<a id="current-milestone-private-discovery-candidate"></a>

## Current milestone: discovery

| Status | Scope |
| --- | --- |
| Included | Search, shared filters, Events/Map/Calendar, read-only entity graphs, event details, provider registration links, profiles and saved filters. |
| Deferred | Chat, automated RSVP, managed handoffs, notifications, Calendar synchronization, purchases and programmatic API keys. |
| Implemented | Guest catalog access, Google accounts and optional Apple support, configured legal acceptance, datastore TLS and Temporal mTLS; GCP Gateway HTTPS and owner-only IAP admin. |
| Recorded deployment | Private discovery with mock product adapters; the [dated runtime record](deploy/development.md#current-deployment) distinguishes serving configuration from prepared source. |
| Release acceptance | Provider/legal-mode configuration, transport/recovery checks and real consumer/admin browser acceptance; [release preparation](https://github.com/iliazlobin/events-concierge/issues/24). |

- `EC_RELEASE_PROFILE=discovery` selects product scope; retained full-profile code does not expand it.
- `EC_MOCK_CLOUD` controls integration behavior independently of product scope.
- Public access exposes the consumer/admin frontends; APIs, stores, GKE nodes and control plane remain private. [Routing and ownership](deploy/public-access.md#route-and-ownership).
- [Consumer accounts](deployment/consumer-identity.md): managed signup, reauthentication/erasure and admin restricted to `iliazlobin91@gmail.com`. The legacy Google-only pilot retains its deletion limitation.
- [Release acceptance](docs/production-operations.md#first-release-acceptance) owns launch gates. Development acceptance does not establish production acceptance.

## Boundaries

- Browsing reads published data; collection runs independently with reviewed sources, bounded requests, pacing and lease-fenced publication.
- Failed refreshes retain the last successful catalog. [History](design/catalog-browse-history.md) is latest-known retained data, not an as-of archive.
- Entity graphs show recorded event mentions, not attendance or social relationships. Names alone never merge identities; imported profile links do not prove fetched profile facts. [Entity contract](design/entity-catalog-and-research.md).
- Discovery exposes entity reads, not external profile refresh. Credentialed enrichment requires separate review and activation. [Enrichment controls](design/entity-enrichment-control-plane.md).
- Consumer, operator and executor authority stay separate. Tenant data remains authorized and RLS-scoped.
- Registration happens on the provider website. Gmail access remains forbidden. [ADR-011](decisions/adr-011-relay-inbox-no-gmail.md).
- Full-product requirements and historical decisions remain in [requirements](design/requirements.md) and [ADRs](decisions/README.md); they do not expand discovery scope.
- [Production operations](docs/production-operations.md#first-release-acceptance) defines acceptance; [release prerequisites](deploy/development.md#remaining-release-work) link tracked work. CI, task acceptance and deployment are separate facts.

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
- Keep scope here, contracts beside code, designs in [Notion](https://app.notion.com/p/391d865005a88164a182eabc18fe068f), operations in runbooks, and task stages in Symphony. Do not copy tracker status into docs.
- [AGENTS.md](AGENTS.md) owns agent boundaries; [WORKFLOW.md](WORKFLOW.md) owns Symphony assignments. Neither grants deployment or provider-mutation authority.
