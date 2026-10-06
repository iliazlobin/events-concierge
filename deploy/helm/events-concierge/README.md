# Events Concierge Helm chart

The chart packages the public Next.js frontend, internal FastAPI API, separately versioned
transactional and catalog Temporal workers, durable worker Deployments, one-shot repair/scanner
CronJobs, a schema migration Job, a bounded catalog dispatcher CronJob, and an opt-in one-source
operational Job. PostgreSQL, Redis, Temporal Server, and shared filesystem volumes are intentionally
absent.

Every container image is assembled as `repository@sha256:digest`; tags are not accepted by the
values schema. Secret Manager values are mounted read-only with the GKE Secret Manager CSI add-on.

For the private shared development landing, layer `values-shared-development.yaml` after
`values-development.yaml`, then apply the target-checked release identity/image values. The data
chart's matching overlay consumes the platform-owned `shared-retain` class without creating it.
The [deployment runbook](../../development.md) owns readiness, state migration, recovery, and
cutover steps; the overlay alone does not establish production readiness.

For authenticated discovery on those in-cluster stores, use
[values-private-authenticated.example.yaml](values-private-authenticated.example.yaml) with chart
defaults and target-checked image/identity bindings. It uses direct PostgreSQL/Redis TLS and four
separate Temporal client certificate Secrets. The cadence process receives only its controller
database DSN and CA. Frontend, catalog and controller processes receive no Google OAuth secrets.
The file does not activate the public edge or IAP admin. Follow the runbook's coordinated TLS
cutover, provider/legal setup and deployed login/logout checks before enabling signup or cadence.

[Public consumer access](../../public-access.md) packages the optional Cloudflare connector
for `events.iliazlobin.com`; admin access remains separate and activation requires a verified release.

Only non-secret identifiers belong in values files. The runtime uses `*_FILE` settings, while the
migration Job receives only its owner URL and application-role bootstrap password files.
Secret references must use immutable numeric versions, never `latest`; the version lists are hashed
into Pod templates so a reviewed version change produces a rollout.
For the managed profile, the Redis URL secret should reference the mounted CA path
`/var/run/secrets/events-concierge/REDIS_CA_CERTIFICATE`; Terraform exports the public CA material
for an audited operator to populate that Secret Manager container. The private profile uses
`/var/run/events-concierge-tls/redis/ca.crt` from its CA-only Kubernetes Secret.

Copy `values-staging.example.yaml` outside version control, replace the example project, identities,
hostnames, secret IDs, and all three image digests with release evidence, then deploy in two phases:

`global.runtimeProviderReady` defaults to and remains `false` in the example. Application and
operations renders fail until an authorized release job has mounted the exact secret versions and
successfully run `python -m events_concierge.operations validate-config` **without**
`--structural-only`; only then may that release's protected values set the gate to `true`. The
full product profile still has unprovisioned provider ports. The bounded discovery profile uses
explicit disabled effects; verify its exact deployed configuration and dependencies before setting
the gate. CI sets it only while proving manifest structure and never deploys.

```bash
chart=deploy/helm/events-concierge
release=events-concierge
namespace=events-concierge-staging
values=/secure/path/staging-values.yaml

kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$namespace" --overwrite \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted

helm upgrade --install "$release" "$chart" \
  --namespace "$namespace" \
  --values "$values" --set global.releasePhase=migration --wait

kubectl --namespace "$namespace" wait \
  --for=condition=complete --timeout=15m \
  --selector=app.kubernetes.io/component=migration job

helm upgrade "$release" "$chart" \
  --namespace "$namespace" --values "$values" \
  --set global.releasePhase=application --wait --timeout=15m
```

The first Helm command creates identity/config/CSI resources and only the revision-suffixed
migration Job. The second removes that completed Job and creates application workloads. Rollback
must reuse previously recorded image digests and must only follow a backward-compatible migration.

The worker liveness probe proves only that PID 1 is alive. Temporal poller count, queue backlog,
schedule-to-start latency, and workflow compatibility remain release and monitoring gates; a
healthy process probe is not evidence that a worker is consuming work.

The optional Gateway expects a TLS Secret created by the chosen certificate controller. Keep it
disabled until the external DNS/certificate/Cloud Armor design is provisioned. PodMonitoring
requires GKE Managed Service for Prometheus, which the Terraform cluster enables.

Bootstrap and label the namespace before the first Helm release as shown above; do not replace that
step with `--create-namespace`, which would omit Pod Security Admission policy labels.

Application-role password rotation is a separate, disabled maintenance operation. Add matching new
numeric versions of `app-role-password` and `database-url`, render `global.releasePhase=role-rotation`
with `jobs.roleRotation.enabled=true`, wait for the role-rotation Job's sanitized success evidence,
then immediately restore `global.releasePhase=application` using the new database URL version. This
phase intentionally removes application workloads while the rotation runs. The single-password
database role has no overlap window: plan a maintenance interval. Rollback requires
running the same gated Job with the previous password version before redeploying the previous DSN;
rolling back only Helm values cannot restore database access.


## Hosted operator profile

`operator.enabled` is disabled by default. The managed profile below uses Cloud SQL and a GKE IAP Gateway.

Prepare the [Terraform foundation](../../../infra/terraform/README.md) with its separate
`operator_enabled = true` opt-in. After authorized provisioning, `tofu output -json
operator_helm_values` provides the chart's two GCP identity annotations and catalog claim-check
prefix; `secret_ids` provides the environment-prefixed database-secret names. Terraform creates
empty secret containers and restricted IAM bindings. It does not populate passwords or secret
versions, assign operators in IAP or `operator.subjectRoles`, or enable this chart profile.
Pin the audited secret versions and keep `operator.executorClaimCheckPrefix` disjoint from the
consumer `EC_GCS_CLAIM_CHECK_PREFIX`.

The deployment requires:

- A distinct operator hostname and TLS Secret, IAP OAuth client ID and existing client-secret Secret,
  exact IAP backend audience, and explicit subject-role map. The chart renders a separate Gateway,
  HTTPRoute, IAP GCPBackendPolicy and health check. IAP IAM access and the application role allowlist
  must both be assigned. See [GKE Gateway IAP configuration](https://cloud.google.com/kubernetes-engine/docs/how-to/configure-gateway-resources#configure_iap).
- Separate non-owner PostgreSQL LOGIN principals belonging to `ec_operator_controller` and
  `ec_ingestion_executor`, created by approved provisioning after migration `0182`. Neither login
  may inherit `ec_app` or owner/elevated privileges. Mount immutable numeric secret versions through
  `operator.operatorSecrets` and `operator.executorSecrets`; these never enter the shared runtime
  SecretProviderClass. The operator API receives only its database URL. The executor receives only
  its own URL, Redis and Temporal credentials; GCS uses its workload identity.
- Distinct `operator-api` and `ingestion-executor` GCP identities, with access limited to their own
  secrets and required Cloud SQL/Redis/Temporal/GCS capabilities. The catalog Temporal workload uses
  the executor identity/profile. The operator frontend receives no database/provider secrets.

The profile starts a dedicated ingestion-command worker and changes the existing hourly catalog
CronJob to enqueue-only `ingestion_cadence --once`. Its controller credential cannot claim or finish
commands. The legacy direct `jobs.catalogRefresh` is rejected; manual work uses operator commands.
The recurring local cadence process and Temporal Schedules are not deployed by this profile.

The API validates the signed IAP assertion even after the proxy; private NetworkPolicy only permits
its operator frontend and GMP. Aggregate `/metrics` is not forwarded by the frontend. PodMonitoring
scrapes queue state and progress timestamps, not worker liveness. Production field proof still needs
IAP grant/revocation, credential separation, command replay/restart/fencing, eventual publication,
alert routing, and the unchanged full-product provider validation gate. Rendering does not activate
these prerequisites or authorize a migration/deployment.

### Private operator

The [authenticated private values](values-private-authenticated.example.yaml) also support the
existing signed-IAP operator app against the retained PostgreSQL service. Enable `operator.enabled`
and both operator workloads only after supplying the separate hostname, TLS/OAuth Secret references,
exact signed-IAP backend audience, and the owner's stable `accounts.google.com:*` subject/role.
The API also requires the signed email `iliazlobin91@gmail.com`; consumer login never grants admin access.

- Supply an isolated operator API GCP identity authorized only for the numbered
  `ec-dev-operator-database-url` Secret Manager version. Its login must retain only
  `ec_operator_controller`. The API mounts that DSN and the PostgreSQL CA, uses `direct_tls`
  with `sslmode=verify-full`, and has no Redis, Temporal, consumer or migration credentials.
- Frontend/API are singletons requesting `25m/128Mi` and `100m/192Mi`, with zero surge and no
  Cloud SQL sidecar. The frontend has no backend secrets or cloud identity. Apply both charts'
  reviewed network policies: the shared data policy excludes both operator components, and the
  application chart supplies only the operator routes.
- The private chart creates no Gateway or OAuth resources. Its current ingress admits only
  Google Front End ranges and its API accepts only signed IAP assertions. HTTP IAP needs a
  separate approved edge/controller change. Cloudflare Access requires a separately reviewed
  JWT verifier and connector routing; Access headers cannot substitute for IAP assertions.
  Preserve existing Symphony OAuth and Hermes tunnel resources.
- Add `--operator` to private `wait_ready.py` and backup creation only when both operator pods
  are installed. Recovery uses the recorded inventory; no extra resume flag. Verify owner login,
  admin reads/commands, non-owner denial, missing/forged assertion denial, CSRF, credential
  isolation and backup/recovery before declaring the endpoint usable. Public consumer routing
  continues to reject `/admin`.
