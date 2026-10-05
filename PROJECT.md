# Events Concierge

Approved product scope. Symphony owns task stages and acceptance; GitHub owns issue content, code and review evidence.

- [Architecture](ARCHITECTURE.md): components, code map and invariants.
- [README](README.md): development commands and workflow.
- [Deployment and recovery](deploy/development.md): serving revisions, configuration, access and recovery evidence.

## Current milestone: private discovery candidate

| Status | Scope |
| --- | --- |
| Included | Search, shared filters, Events/Map/Calendar, read-only entity graphs, event details, provider registration links, profiles and saved filters. |
| Deferred | Chat, automated RSVP, managed handoffs, notifications, Calendar synchronization, purchases and programmatic API keys. |
| Implemented; activation pending | Google sign-in for provisioned identities, datastore TLS and self-hosted Temporal mTLS. |
| Accepted development behavior | Private discovery and recovery using mock external product adapters. |
| Production acceptance pending | OAuth configuration, private HTTPS and deployed identity/transport verification. |

- `EC_RELEASE_PROFILE=discovery` selects product scope; retained full-profile code does not expand it.
- `EC_MOCK_CLOUD` controls integration behavior independently of product scope.
- Google self-service account deletion requires independent reauthentication, which remains unsupported.
- A private pilot accepting unavailable self-service deletion needs an explicit owner decision.
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
