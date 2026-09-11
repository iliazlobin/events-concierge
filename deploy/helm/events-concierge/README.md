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

Only non-secret identifiers belong in values files. The runtime uses `*_FILE` settings, while the
migration Job receives only its owner URL and application-role bootstrap password files.
Secret references must use immutable numeric versions, never `latest`; the version lists are hashed
into Pod templates so a reviewed version change produces a rollout.
The Redis URL secret should reference the mounted CA path
`/var/run/secrets/events-concierge/REDIS_CA_CERTIFICATE`; Terraform exports the public CA material
for an audited operator to populate that Secret Manager container.

Copy `values-staging.example.yaml` outside version control, replace the example project, identities,
hostnames, secret IDs, and all three image digests with release evidence, then deploy in two phases:

`global.runtimeProviderReady` defaults to and remains `false` in the example. Application and
operations renders fail until an authorized release job has mounted the exact secret versions and
successfully run `python -m events_concierge.operations validate-config` **without**
`--structural-only`; only then may that release's protected values set the gate to `true`. The
repository's current partial GCP provider does not pass that gate, so it must not be enabled merely
to make a render succeed. CI sets it only while proving manifest structure and never deploys.

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

`operator.enabled` is disabled by default. Enable it only with a reviewed deployment configuration.

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
