# Private transport

Datastore TLS, Temporal mTLS, certificate custody and coordinated conversion. Requires [verified private access](access.md#connect), approved configuration changes and [recovery](recovery.md). Preparation and offline checks do not activate this profile.

## Authenticated discovery and encrypted dependencies

- Opt-in [data TLS values](../../deploy/helm/events-concierge-dev-data/values-private-tls.yaml) and [Temporal TLS values](../../deploy/helm/temporal-private-tls.yaml).
- [Authenticated application values](../../deploy/helm/events-concierge/values-private-authenticated.example.yaml) use the existing stores with Google Identity Platform, verified TLS and process-specific Temporal client keys. Use this file with chart defaults, not the demo overlays. Provisioning and deployed acceptance are separate release steps.
- The private profile disables demo admin and deferred product effects. The opt-in [private operator](../../deploy/helm/events-concierge/README.md#private-operator) uses the isolated controller credential; its signed identity and separate edge still need release acceptance. Consumer sign-in never grants an admin role.

| Connection | Required contract |
| --- | --- |
| Application and operator PostgreSQL | Service FQDN, `sslmode=verify-full`, `PGSSLROOTCERT` pointing to a CA-only mount; preserve each process's restricted DB role |
| Temporal PostgreSQL | Service FQDN in both SQL `connectAddr` values, verified TLS and CA-only mount; restored database/schema/namespace initialization stays disabled |
| Application Redis | `rediss`, trusted CA file, required certificate verification and hostname checking; existing password authentication |
| Application to Temporal | Frontend Service FQDN, server CA and separate client certificate/key files with `clientAuth` usage |
| Temporal internode and internal frontend clients | Server certificate with `serverAuth` and `clientAuth`, trusted client CA, verified server names |

- Datastore Secrets: `ca.crt`, `tls.crt`, `tls.key`; exact Service FQDN DNS SAN; short common name.
- Temporal server SAN also includes `ec-dev-temporal-internode`.
- `ec-dev-postgres-ca-v1`: **only** `ca.crt`; never distribute PostgreSQL server keys to clients.
- Signing keys outside workloads/Git; versioned credentials per workload; no Google/OIDC secrets in operator/catalog processes.
- PostgreSQL/Redis stage keys in memory with native ownership/`0600`; rotate Secret names and roll pods.
- Redis 7 probe `--sni` does not verify hostnames; application clients must.

**Prepare one certificate generation locally**

```bash
mkdir -p .local/private-tls
.venv/bin/python scripts/development/private_tls.py --output .local/private-tls/generation-1
```

- The new directory is `0700`; files are `0600`. Keep signing keys in `authorities/`; never mount them in workloads. Recovery custody uses GCP Secret Manager in `iz27-platform-dev`: `ec-dev-postgres-ca-recovery-g1`, `ec-dev-redis-ca-recovery-g1`, `ec-dev-temporal-ca-recovery-g1`.
- Each recovery JSON contains `ca_certificate_pem`, `ca_private_key_pem`, generation, project, namespace and that issuer's public leaf inventory. Exclude leaf keys and database credentials. Pin numeric Secret versions; verify certificate/key matching and exact recovery readback before using this CA for retained data. Keep the protected custody receipt outside Git.
- Grant recovery access only to the operator; verify Secret-level and inherited IAM. Applications and workers get no recovery access. Keep payloads out of Terraform state. Set a 30-day version destruction delay and no automatic expiry; this delay does not protect whole-Secret deletion.
- `leaves/` contains eight Kubernetes Secret bundles; root `inventory.json` records fingerprints and expiry. Create these as **immutable**, versioned Secrets in `events-concierge-dev` through the verified private access context. Use `ca.crt`, `tls.crt`, `tls.key` from the matching leaf only.
- Create three CA-only immutable Secrets: `ec-dev-postgres-ca-v1`, `ec-dev-redis-ca-v1`, `ec-dev-temporal-ca-v1`. They contain only the matching `authorities/<store>/ca.crt`. No application, migration or cadence process gets a datastore server key.
- Leaves expire after 90 days; review/rotate before 14 days remain. Use `--generation 2` and a new output directory for the next version. The issuer creates fresh roots: rotate server, client and trust Secrets together during reviewed maintenance, verify rejection tests, then retire unused generations. No automatic expiry alert or renewal is provided.
- Populate new numbered database DSN versions with the existing passwords/roles, service FQDN and `sslmode=verify-full`. `PGSSLROOTCERT=/var/run/events-concierge-tls/postgres/ca.crt` is mounted for each SQL consumer, including migration/cadence. Redis DSNs use `rediss` with `ssl_ca_certs=/var/run/events-concierge-tls/redis/ca.crt`, `ssl_cert_reqs=required` and `ssl_check_hostname=true`. Never print DSNs or put them in Helm values.
- API and erasure use the consumer DB role; executor/catalog use the restricted ingestion role; cadence uses the controller role. Pin actual Secret Manager version numbers; the example's `2` is not proof that a matching version exists. Review each process's IAM access and keep Google provider OAuth secrets outside workloads.
- This profile reuses retained databases and established restricted roles. A fresh installation needs its separately reviewed role bootstrap before migration; the TLS migration Job does not create operator/ingestion credentials.
- The separate data chart supplies default-deny and explicit Pod-selector routes for stores, Temporal, DNS and metadata access. On Dataplane V2, private IP CIDRs do not admit Pod traffic. Verify both kube-dns and NodeLocal DNSCache connectivity before cutover.
- Enable the [GCP public edge](public-access.md) only after transport verification. The frontend sidecar preserves consumer route/header filtering; GFE ingress is limited to consumer/admin frontend ports. Review both charts together: Kubernetes allow rules are additive.

**Validate without cluster changes**

```bash
helm template ec-dev-data deploy/helm/events-concierge-dev-data -n events-concierge-dev -f deploy/helm/events-concierge-dev-data/values-shared-development.yaml -f deploy/helm/events-concierge-dev-data/values-private-tls.yaml
helm template ec-dev-temporal temporal --repo https://go.temporal.io/helm-charts --version 1.6.0 -n events-concierge-dev -f deploy/helm/temporal-development.yaml -f deploy/helm/temporal-private-tls.yaml
EC_HELM_BINARY=helm EC_DATA_TLS_DOCKER=1 .venv/bin/python -m pytest tests/unit/test_development_data_tls.py -q
EC_HELM_BINARY=helm .venv/bin/python -m pytest tests/deployment/test_authenticated_private_runtime.py tests/deployment/test_private_certificate_issuance.py tests/deployment/test_temporal_mtls_transport.py -q
```

**Coordinated maintenance**

1. Review Secret/IAM/mounts, certificate lifetimes and recovery access. Verify SCRAM for every login; record booleans, never hashes.
2. Check CPU/memory for workloads, cadence, migration and rollout overlap. Resolve scheduling failures; no Symphony-worker tolerations.
3. Rehearse transport conversion/rollback; verify backup; stop cadence and all writers; inventory Redis state.
4. Preserve PVCs/data; never initialize restored Temporal databases. Change listeners and clients together.
5. Require SQL queries, Redis commands and actual Temporal mTLS handshake before writers.
6. Require rejection of plaintext, wrong CA/hostname and missing/untrusted client certificates; complete authenticated application acceptance.
   Verify real Google login, reload, personal-data writes, logout and rejection of the revoked session on the trusted deployed origin. Configure the provider project/client and the approved legal mode before enabling signup; browser fixtures are not IdP acceptance.
7. Rollback: stop writers; restore listener/client configuration together; retain certificates/PVCs; verify readiness and queues before resume.

Authenticated private release gates select the profile explicitly; keep cadence disabled until promotion and acceptance:

```bash
.venv/bin/python scripts/development/wait_ready.py --target shared --profile private
kubectl -n events-concierge-dev exec -i deployment/events-concierge-api -- python - --profile private < scripts/development/promote_workers.py
```

- The private gate requires five singleton application Deployments and rejects demo, deferred or unknown Deployments. Promotion uses runtime mTLS and requires recent workflow and activity pollers for the exact candidate build. `smoke.py` remains a synthetic demo check; use the authenticated browser acceptance above.
- Public access shares one frontend Deployment and hostname. After admin activation run `wait_ready.py --target shared --profile private --operator --shared-frontend`; verify Gateway/routes, certificate, backend IAP/IAM and browser acceptance separately before DNS publication. Historical tunnel receipts remain recoverable; the chart installs no connector.
- Add `--operator --shared-frontend` to private backup creation for publicEdge. Legacy private installations with both separate operator workloads use `--operator` alone. These flags select exact inventories; they do not verify browser authentication or create an edge. Resume reads the saved inventory without extra flags.
- For an authenticated private backup, add `--hold-stopped` to the following creation command for cutover. Replace `SET_ID` with its exact completed prefix; verification uses the saved data, and interrupted recovery selects the saved profile. The [demo recovery examples](recovery.md#manual-recovery) retain their default profile.

```bash
.venv/bin/python scripts/development/backup.py backup --target shared --profile private
.venv/bin/python scripts/development/backup.py verify gs://iz27-platform-dev-ec-backups/SET_ID --target shared
```

- Cadence's 90-second deadline includes scheduling.
- Local TLS tests do not prove GKE delivery/authorization; [Temporal authorization limits](../runtime.md#temporal).
