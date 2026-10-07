# Events Concierge

Discover events, browse Events/Map/Calendar and entity graphs, and open provider registration or hand selected free events to Muse.

## Runtime

- **Selected scope:** discovery, profiles, saved filters and [Muse signup handoffs](docs/operations/muse-signups.md); `EC_RELEASE_PROFILE=discovery`. Chat, internal automated RSVP, notifications, calendar sync, purchases and general API keys remain deferred.
- **Local:** full development profile with `EC_MOCK_CLOUD=true`. Public-source refreshes can still make real network requests. Mock identity is unsuitable for public traffic.
- **GCP:** private workloads on shared `platform-dev`. The application owns releases, identities, collection and stores; [gcp-foundation](https://github.com/iliazlobin/gcp-foundation) owns projects, network, cluster and access VM.
- **Authenticated discovery:** selected values use Identity Platform with legacy OIDC disabled. Missing production bindings fail closed; full-profile production bindings remain incomplete.
- **Release:** immutable reviewed artifacts, provider/transport/recovery checks and deployed browser acceptance. Source implementation and healthy Pods do not prove activation.

## Documentation

| Purpose | Home |
| --- | --- |
| Product scope and conventions | [PROJECT.md](PROJECT.md) |
| Code map, flows and invariants | [ARCHITECTURE.md](ARCHITECTURE.md), [runtime](docs/runtime.md) |
| Local setup, branches and checks | [Development](docs/development.md) |
| Support, release, access and recovery | [Operations](docs/operations/README.md) |
| Collection policy and commands | [Ingestion administration](docs/ingestion-admin.md) |
| Implementation contracts and decisions | [design/](design/), [decisions/](decisions/) |
| Contributor boundaries and Symphony handoffs | [AGENTS.md](AGENTS.md), [WORKFLOW.md](WORKFLOW.md) |

[Notion design](https://app.notion.com/p/391d865005a88164a182eabc18fe068f) explains architecture and rationale; GitHub owns executable contracts and runbooks.
