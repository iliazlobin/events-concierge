# Recovery

Application state boundaries, store installation, backup, interrupted-run recovery and restore drills. Select the installed [transport profile](transport.md); every mutating procedure requires authorization.

## Application landing

| Boundary | Requirement |
| --- | --- |
| App Terraform | [shared-development](../../infra/terraform/environments/shared-development); no cluster/network creation |
| Input / review | Verified `platform_contract`; saved-plan check: `scripts/development/check_shared_plan.py` |
| State | Fresh app state → `iz27-platform-dev-ec-state`; [backend setup](../../infra/terraform/environments/shared-development/backend.tf.example) |
| Storage | Platform-installed `shared-retain`; data chart `createStorageClass=false`, `storageClass=shared-retain` |
| Helpers | Default to shared; retired target rejected; `--target shared` remains accepted |
| Values | Authenticated release: chart defaults → private authenticated values → generated image/identity bindings → reviewed consumer/edge overrides. Demo only: development → shared overlay → generated bindings |
| Workers | Catalog/erasure active; transactional, request-start, notification and change-delivery disabled |

- Keep state/plans/credentials outside Git; never initialize the retired target's backend for this destination.
- The separate [development root](../../infra/terraform/environments/development) manages resources awaiting verified retirement.
- Remove that root only after data/image/identity dependencies are resolved and its owning state is empty.
- Existing stores: skip installation below; storage changes require separate rehearsal.
- The private operator uses the dedicated `ec-dev-operator-api` GSA, bound only to `events-concierge-dev/events-concierge-operator-api` with database access only to `ec-dev-operator-database-url`. The application identity root separately grants [RBAC policy reads](operator-access.md). This root grants it no consumer database, Redis, object or project IAM access. For an existing landing, check the saved plan with `scripts/development/check_shared_plan.py --operator-prerequisite`; only this GSA and its two IAM resources may be added, with all existing resources unchanged. Apply requires separate approval; generated release values then include the operator identity.

**New stores / coordinated restoration only**

- First complete [Connect](access.md#connect); verify the dedicated shared kubeconfig, expected context and Ready nodes.

```bash
kubectl create namespace events-concierge-dev --dry-run=client -o yaml | kubectl apply -f -
.venv/bin/python scripts/development/bootstrap_secrets.py --target shared --project-to-kubernetes
helm upgrade --install ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml --wait --timeout 10m
```

1. Require TCP readiness and empty application/Temporal databases before restoration.
2. Create restricted `ec_app` with its pinned password before restore; migration 0002 will not rerun.
3. Create NOLOGIN `ec_operator_viewer`, `ec_operator_controller`, `ec_ingestion_executor`; create NOINHERIT `ec_operator_aggregate_definer`.
4. Grant viewer to controller; require `ec_owner`, aggregate-definer and `temporal` owners; leave `ec_dev_*` logins for bootstrap.
5. Restore verified dumps with `pg_restore --exit-on-error` and exact payload hierarchy; preserve owners, ACLs and provenance.
6. Disable Temporal database/schema/namespace initialization; run candidate migrations/bootstrap before writers.
7. Verify counts, isolation and historical payload reads; never substitute test fixtures.

- Redis excluded: inventory sessions, admission fences and backoffs; preserve or wait out backoffs before cold start.
- Current backups hash every payload object; older payload-only sets have narrower coverage.
- After restoration, while application and Temporal writers remain absent:

```bash
.venv/bin/python scripts/development/check_store_recovery.py --target shared
```

## Persistent storage and self-healing

| Store | Persistence | Limit |
| --- | --- | --- |
| App PostgreSQL | Retained 20 GiB `pd-balanced` | Zonal; manual backup |
| Temporal PostgreSQL | Retained 20 GiB disk | Zonal; coordinated workflow backup |
| Redis | Retained 10 GiB; AOF every second + RDB; `Recreate` | About one second crash loss; excluded from database/payload backup |
| Avatars | Private `iz27-platform-dev-ec-media` | No versioning, soft delete or retention; excluded from backups |

- Pod replacement preserved markers/PVC/PV bindings; not proof of disk/zone-loss recovery.
- Startup probes permit recovery; controllers restart pods; GKE repairs/upgrades nodes.
- One zone (`us-west1-a`); repair/replacement can cause downtime. Retained disks are not backups.
- Redis disk loss needs separate restoration/reconstruction.
- Avatar access: API, private admin and erasure worker; adapter validates deletion policy.
- Restore can require avatar reupload; verify upload, cross-replica read, deletion and erasure separately.

## Manual recovery

**Backup**

1. Use shared kubeconfig; record cadence suspension state; suspend scheduled jobs.
2. Wait for unfinished Jobs and active ingestion commands; inspect admin command/run views.
3. Require no active product workflows, catalog/command leases or pending request/notification work; recheck after writers stop.

```bash
kubectl -n events-concierge-dev patch cronjob events-concierge-ingestion-cadence --type=merge -p '{"spec":{"suspend":true}}'
```

**Normal backup — restores writers automatically**

```bash
.venv/bin/python scripts/development/backup.py backup --target shared
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
.venv/bin/python scripts/development/wait_ready.py --target shared
```

**Release/cutover — keep writers stopped**

```bash
.venv/bin/python scripts/development/backup.py backup --target shared --hold-stopped
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Run either backup path only after Jobs and collection commands finish.
- Held-stopped path: run readiness only after migration and application restart; follow [release](release.md#gke-release).
- Replace `SET_ID` with exact completed prefix printed by backup.
- **Normal backup:** stop app writers then Temporal; retain PostgreSQL/Redis; save three databases, payloads and image/schema metadata.
- **Private recovery:** close the shared frontend first and wait for its Pods to disappear; stop writers and Temporal. Restore Temporal/writers and both APIs before reopening the shared frontend after guarded exact readiness. `--operator --shared-frontend` records this inventory; legacy `--operator` retains the separate-pair ordering. Unknown/partial inventories block backup. Failures close the edge; changed ownership or failed closure needs operator recovery. Resume uses saved mode, replicas, identities and templates.
- `recovery.json`: saved/read back before stopping writers; completion marker last; normal backup restores original replicas.
- Verification: disposable Docker restore and checksums; only completed sets qualify. Recreate schema-required password-free roles only in the disposable container; preserve owners/ACLs and verify privilege and tenant isolation.
- Retain at least three successful sets; no automatic deletion; same project boundary, no independent project-loss protection.
- Excludes Redis/private avatars. Never restore over running stores.
- Restore prior cadence state after readiness/verification; `resume` restores no CronJob schedules.
- Restart forwards after pod replacement.

**Interrupted backup — before migration or rollout**

```bash
.venv/bin/python scripts/development/backup.py resume gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Restore connectivity first; exact prefix printed before quiescing.
- Repeatable; Temporal first; restores replicas only, not data/images/schema.
- Refuses unknown names, writer replicas outside 0/1 (private public edge 0/2), changed schema/Deployment identities/pod templates; private recovery also refuses a changed Deployment inventory. Investigate, never force.
- Preserve incomplete prefixes as evidence; never restore their data.
- Older sets without `recovery.json`: reviewed manual recovery from saved manifest.
- After migration starts: compatible-image/coordinated-data recovery; `resume` is not rollback.
- Optional development load: `smoke.py --repeat 12` inside API pod; not production disaster-recovery acceptance.

## Restore drill

- Private recovery commands/evidence: [backup and restore](#manual-recovery). Repeat for the candidate; earlier demo restoration is not authenticated-candidate acceptance.
- `make restore-drill-local` **starts Compose and migrates**; authorized isolated environment only, writers stopped. Uses disposable `ec_restore_drill_*`, verifies schema/aggregates/sequences/roles/FORCE RLS/two-tenant isolation, removes temporary dump/database; no provider workers.
- Local drill does not prove PITR, key/Temporal/payload recovery or production RPO/RTO.

1. Record last acknowledged transaction/recovery timestamp; start clock. Restore DB and matching payload versions in a fenced environment with recovery-only keys and no real provider access.
2. Verify integrity, non-owner RLS, queue/audit continuity and no secrets. Replay representative histories with compatible builds; digest-check claim checks.
3. Start guarded workers against test endpoints; prove leases/idempotency, authenticated canary and readiness. Stop clock at serving recovery.
4. Record gaps, remove restored data under retention policy; preserve sanitized evidence. Deferred full-runtime drill also proves engine outage → durable pending intake → one resumed parent/request without duplicate effects.

**Production target, not a private-stack claim:** [NFR-13](../../design/requirements.md#5-non-functional-requirements) requires RPO ≤60 seconds, zero acknowledged-transaction loss, serving API RTO ≤24 hours and rebuildable projections. Production topology/PITR, key/history/payload recovery and recurring drill evidence remain separate acceptance work.
