# Events Concierge Helm chart

The chart packages the public Next.js frontend, internal FastAPI API, separately versioned
transactional and catalog Temporal workers, durable relay Deployments, one-shot repair/scanner
CronJobs, a schema migration Job, a bounded catalog dispatcher CronJob, and an opt-in one-source
operational Job. PostgreSQL, Redis, Temporal Server, and shared filesystem volumes are intentionally
absent.

Every container image is assembled as `repository@sha256:digest`; tags are not accepted by the
values schema. Secret Manager values are mounted read-only with the GKE Secret Manager CSI add-on.
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
