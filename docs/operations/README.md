# Operations

Application support and procedures on shared GKE. Start with [support](support.md) during an incident; choose the installed profile before acting.

## Ownership

| Owner | Responsibility |
| --- | --- |
| Events Concierge | Consumer/admin routes, accounts/RBAC, collection, workload configuration, migrations, stores and backups |
| [Shared platform](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/support.md) | Projects/IAM boundaries, Terraform state, VPC/NAT, GKE nodes, access VM and platform costs |

Application Terraform must not adopt platform state or cluster/network resources. Application credentials, rollout and data recovery are separate from platform access.

## Procedures

| Case | Runbook |
| --- | --- |
| Diagnose an incident | [Support](support.md) |
| Connect or reopen a private page | [Access](access.md) |
| Publish artifacts, release or roll back compatible images | [Release](release.md) |
| Install/restore stores, back up or recover an interrupted backup | [Recovery](recovery.md) |
| Convert transport or rotate private certificates | [Private transport](transport.md) |
| Configure consumer signup and erasure | [Consumer identity](consumer-identity.md) |
| Connect Muse and hand off free-event signups | [Muse signups](muse-signups.md) |
| Configure operator roles and policy versions | [Operator access](operator-access.md) |
| Prepare, activate or contain public HTTPS/IAP | [Public access](public-access.md) |
| Contain provider effects or rotate credentials | [Maintenance](maintenance.md) |
| Investigate source commands and coverage | [Ingestion administration](../ingestion-admin.md) |

[Runtime](../runtime.md) defines processes, health signals and limits. Charts/Terraform keep resource configuration beside their code; procedures link those artifacts rather than duplicating them.

## Release record

The [existing release record](https://github.com/iliazlobin/events-concierge/issues/26) owns serving revisions, profiles, acceptance and recovery evidence. [Observability](https://github.com/iliazlobin/events-concierge/issues/23) tracks monitoring work. Check those records and live state before operating; do not infer activation from the newest source.

Read-only diagnosis comes first. Deployment, source activation, migration, restore, access changes and infrastructure applies require their corresponding authorization. Keep tokens, identities, provider bodies and private configuration out of incident evidence.
