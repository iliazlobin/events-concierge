# Application support

Common incidents on shared GKE. Use the deployed profile and [release record](https://github.com/iliazlobin/events-concierge/issues/26), not the newest source example, to choose recovery steps. Public routing and authenticated accounts need their own activation evidence.

| Symptom | Start here |
| --- | --- |
| Page unavailable, loopback connection refused, public gateway error | [Access and routing](#access-and-routing) |
| Google sign-in fails or admin access is denied | [Sign-in and operator access](#sign-in-and-operator-access) |
| Missing events, stale sources or stuck refresh | [Collection](#collection) |
| Events load but have no map markers | [Map locations](#events-missing-from-the-map) |
| API not ready, database/Redis/Temporal failure | [Stores and background workers](#stores-and-background-workers) |
| Failed rollout or interrupted backup | [Release and recovery](#release-and-recovery) |
| Certificate warning or transport failure | [Certificates](#certificates) |
| Account deletion remains pending | [Account erasure](#account-erasure) |
| Pending/OOM Pods, growing disks, unexpected spend | [Platform support](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/support.md) |

## Start here

1. Record UTC start time, affected view/source, last successful operation, serving revision and recent changes in the existing issue or release record. Use opaque command/run references; exclude personal data, tokens and provider bodies.
2. Connect through [platform private access](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/access.md). Use its dedicated `KUBECONFIG`; verify CLI authentication separately from browser access.
3. Check the intended cluster and namespace before inspecting workloads:

```sh
EC_CONTEXT=gke_iz27-platform-dev_us-west1-a_platform-dev
EC_NAMESPACE=events-concierge-dev
test "$(kubectl config current-context)" = "$EC_CONTEXT" || exit 1
kubectl --context="$EC_CONTEXT" -n "$EC_NAMESPACE" --request-timeout=20s \
  get deployments,statefulsets,pods,jobs,cronjobs,pvc
kubectl --context="$EC_CONTEXT" -n "$EC_NAMESPACE" --request-timeout=20s \
  get deployments -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,IMAGES:.spec.template.spec.containers[*].image'
```

These are read-only checks. Inspect logs privately, with a specific workload and short time window; redact before sharing. Avoid full manifests, environment/config dumps, Secret reads and `helm get values --all` as incident evidence.

Source refresh, Pod deletion/restart, migrations, backup/restore, access changes and infrastructure applies need the corresponding operational authorization. Escalate an uncertain destination, unexpected workload or unknown recovery state; do not force a repair.

## Access and routing

**Private access:** a refused `127.0.0.1` connection usually means its local forward is absent. The address works only on the forwarding machine.

- Check the IAP connection and dedicated kubeconfig first. Reconnect using the platform procedure if the cluster is unreachable.
- Restart only the affected loopback forward after reconnecting or Pod replacement; a Service forward stays attached to its selected Pod. Use one terminal per forward:

```sh
kubectl --context="$EC_CONTEXT" -n "$EC_NAMESPACE" port-forward \
  --address=127.0.0.1 service/events-concierge-api 14000:8000
```

- Consumer forward: `service/events-concierge-frontend 14001:80`. Demo admin forward: `deployment/events-concierge-admin 14002:3000`, only when that Deployment belongs to the installed demo profile. Authenticated admin uses its reviewed IAP route.
- With the API forward running, inspect process, dependencies and serving revision:

```sh
curl --silent --show-error --max-time 10 --write-out '\nHTTP %{http_code}\n' \
  http://127.0.0.1:14000/healthz
curl --silent --show-error --max-time 10 --write-out '\nHTTP %{http_code}\n' \
  http://127.0.0.1:14000/readyz
curl --silent --show-error --max-time 10 --write-out '\nHTTP %{http_code}\n' \
  http://127.0.0.1:14000/versionz
```

**Public access, if activated:** inspect Gateway/HTTPRoute conditions, backend health and certificate state through [public-edge operations](public-access.md#operate). Verify DNS and the exact HTTPS hostname; separate an edge failure from a private API failure. Private health/version/metrics endpoints intentionally have no public route; their public `404` is expected.

**Verify:** the selected page loads, the private API reports the expected revision, and required dependencies are ready. `/healthz` alone proves only that the process responds. If exposure is unsafe, follow [edge containment](public-access.md#activate); withdrawing DNS alone leaves direct-IP access possible. Preserve private recovery access and controller ownership.

## Events missing from the map

Map markers and day counts describe the loaded pages. Compare the loaded count with the
full matching count, then choose **Load remaining events**; each action reads at most 40
pages of 100 events and retains successful pages if a later request fails. Large catalogs
keep a continuation. Events without a reviewed map location remain in the separate list.
Compare provider IDs across every catalog page before diagnosing an ingestion gap.

Compare the same filters in Events and Map, then inspect `latitude`/`longitude` in the
`GET /v1/catalog/events` response. Loaded events with null coordinates are a location-data gap;
a missing base map points to tile delivery or browser rendering instead.

SF Tech Week 2026 publishes area names rather than venue coordinates. The frontend groups
reviewed SF areas into outlined count markers labeled **approximate**; click one to browse
its events. Provider coordinates take precedence. Virtual events, `Other`, unrecognized areas
and sources without a reviewed area mapping stay in the unlocated list. These display centers
never change stored coordinates, distance filters or directions. Verify desktop/mobile selection
and pagination before release; no database backfill or fresh crawl is needed for this fallback.

## Sign-in and operator access

| Failure | Diagnose | Recovery and verification |
| --- | --- | --- |
| Google sign-in | Check the deployed origin/auth domain, enabled provider, signup setting and browser-key restrictions; inspect identity readiness and provider quotas. | Follow [consumer identity](consumer-identity.md#gcp-setup). Verify cancellation, login/reload/logout and saved filters on the trusted origin. Never substitute mock identity or bypass TLS/CSRF. |
| IAP denies entry | Check the protected backend and approved IAP grant. Consumer login is unrelated. | Follow [admin setup](public-access.md#admin); verify authorized entry and unassigned-user rejection. Do not broaden project-wide access as a repair. |
| Operator API `401` / `403` | Invalid assertion vs. unassigned identity or insufficient capability; check the actual backend audience and pinned policy version. | Follow [RBAC configuration](operator-access.md#configuration); verify the intended role and denied actions. Never copy assertions or private bindings into a ticket. |
| Operator API `503` | Check its separate readiness: policy access/validity and restricted database readiness. A healthy shared frontend is insufficient. | Restore an enabled, reviewed policy/configuration through [publish and activate](operator-access.md#publish-and-activate). No stale grant fallback; never restore revoked access. |

**Consumer sign-in `401`:** private `/metrics` exposes `events_concierge_consumer_sign_in_rejections_total{reason="..."}`. Fixed categories distinguish origin, cookie/challenge, token/claims, reauthentication and account rejection. Compare counters before/after one controlled attempt on the same Pod and process; they are cumulative and reset on restart. No tokens, cookies, account IDs or provider errors enter these labels. The browser keeps its generic error; `503` dependency failures do not increment this counter.

For operator readiness, forward `service/events-concierge-operator-api 14003:8000` using the scoped command above and inspect `http://127.0.0.1:14003/readyz`. Use only when that service is deployed; it requires no assertion for this private readiness probe.

## Collection

**Diagnose**

1. In **Sources**, check admission, review expiry, pause/quarantine, last success, due time and collection window. Compare consumer results using the same city/date/source filters.
2. Follow **Track command** into source tasks and runs. A queued command or completed dispatch is not a successful publication. Inspect [status meanings](../ingestion-admin.md#investigate-work), leases, retry budget and page-cap failures.
3. Check executor/catalog worker readiness and cadence scheduling:

```sh
kubectl --context="$EC_CONTEXT" -n "$EC_NAMESPACE" --request-timeout=20s \
  get cronjob events-concierge-ingestion-cadence \
  -o custom-columns='NAME:.metadata.name,SUSPENDED:.spec.suspend,LAST:.status.lastScheduleTime'
```

**Recover:** after resolving the cause, an authorized operator may queue one reviewed source refresh through [ingestion administration](../ingestion-admin.md). Keep source revisions, policy, pacing, leases and attempt budgets. A blocked source or platform needing developer access requires its own activation; do not bypass guards or mark truncated results complete.

**Verify:** successful run → fenced publication → matching consumer results, with the original window and build evidence. A valid empty feed can succeed with zero events. Failed refreshes retain the previous catalog; repeated publication totals do not prove platform completeness. Use the [read-only coverage check](release.md#gke-release) for the approved database after publication; it starts no services or migrations.

## Stores and background workers

- Inspect scoped Pod/PVC state, restarts and resource pressure. Route node, scheduling, disk and egress failures to [platform support](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/support.md); the application owns its stores and data.
- Read the private API readiness components. Database or configured identity unavailability returns `503`. Temporal can be `degraded` with HTTP `200`; inspect engine and catalog workers separately. Redis failure can block identity readiness and collection pacing.
- Match required processes to the installed [process inventory](../runtime.md#required-process-inventory). Do not start deferred transactional workers to repair discovery.
- Recover through [coordinated maintenance](transport.md#authenticated-discovery-and-encrypted-dependencies) or [manual recovery](recovery.md#manual-recovery). Preserve PVCs, credentials, fenced commands and Temporal history; do not initialize restored databases or reset queues.

**Verify:** required dependencies and workers recover, readiness components agree, and one approved collection command publishes without duplicate effects. Retained disks survive Pod replacement but do not provide zone-loss recovery; [storage limits](recovery.md#persistent-storage-and-self-healing) govern escalation.

## Release and recovery

**Diagnose:** compare serving revision/digests, rollout replicas and migration result with the release record. Check the exact completed, verified backup set and any saved `recovery.json` before restarting writers. Inspect the installed profile; source examples are not rollback values.

**Recover:** use [release and access](release.md#gke-release) and [manual recovery](recovery.md#manual-recovery). Keep cadence stopped until acceptance; restore its prior state afterwards. A compatible image/configuration rollback does not reverse database migrations. Overwriting newer writes requires separate approval.

| Helper | Effect / limit |
| --- | --- |
| `wait_ready.py` | Read-only replica/generation inventory; select the installed profile. Does not verify API/TLS/login/collection. |
| `backup.py backup` | Quiesces writers and writes GCS; save original replicas and cadence state. |
| `backup.py verify` | Downloads private data, restores disposable Docker databases and writes a verification receipt to GCS. |
| `backup.py resume` | Restores saved replicas only, before migration/rollout; does not restore data, images or CronJob schedules. |
| `check_store_recovery.py` | Recovery drill: writes markers and deletes store Pods. |
| `smoke.py` / `promote_workers.py` | Write synthetic data / change Temporal routing; release procedures only. |

**Verify:** compatible schema/images, restored privilege/tenant boundaries, exact workload inventory, authenticated browser acceptance and real collection publication. Record measured recovery time and any lost data; earlier drills do not prove recovery of this release. Redis and avatars are excluded from the coordinated database/payload backup; assess them separately before resume.

## Certificates

- Public edge: inspect [Certificate Manager](https://console.cloud.google.com/security/ccm/list/certificates?project=iz27-platform-dev&authuser=4) and DNS authorization; Google renews public certificates while authorization remains valid.
- Private transport: check reviewed public certificate files, never keys. For each server/client leaf used by the installed profile:

```sh
openssl x509 -in "${EC_CERT_FILE:?Set the approved public certificate path}" \
  -noout -dates -checkend 1209600
```

- This warns within 14 days of expiry; private leaves last at most 90 days. Expiry checks alone do not prove current mounts or connectivity.
- Rotate trust, server/client Secrets and consumers together through [encrypted-dependency maintenance](transport.md#authenticated-discovery-and-encrypted-dependencies). Verify actual SQL/Redis/Temporal TLS and rejection of plaintext, wrong host/CA and untrusted clients before reopening writers. Never bypass certificate validation.

## Account erasure

- An accepted deletion means cleanup is pending. Inspect the account-erasure worker's readiness and bounded, sanitized failure evidence through approved private access; retain the opaque request reference and elapsed time.
- Identity/provider or cleanup failures require repair of the scoped dependency/credential, then the existing fenced worker retry. Do not remove tombstones, recreate sessions or mark cleanup complete manually. [Erasure contract](consumer-identity.md#access).
- Before a data restore, review deletion fences and later erasure requests so restored records cannot revive deleted accounts. Keep serving closed until that review and recovery checks complete.

**Verify:** identity removal and application cleanup both completed, revoked sessions are rejected, and no follow-up worker is still pending. Escalate missing evidence instead of inferring completion from HTTP acceptance.
