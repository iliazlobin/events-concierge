# Public access

- Consumer target: [events.iliazlobin.com](https://events.iliazlobin.com); anonymous catalog browsing, Google accounts, chat disabled.
- Admin target: [events.iliazlobin.com/admin](https://events.iliazlobin.com/admin); Google IAP admits the configured administrator. The operator API separately verifies the signed identity and [RBAC policy](operator-access.md).
- One hostname, global external Application Load Balancer, reserved IPv4, managed certificate and Next.js Deployment. No redirect or separate admin web process.
- GKE nodes, control plane, API and databases stay private. Consumer sign-in grants no admin role.
- Rendering, healthy Pods and green CI do not establish public or browser acceptance.
- Hostname publication and acceptance are tracked in [release preparation](https://github.com/iliazlobin/events-concierge/issues/24); these target links do not establish availability.

## Route and ownership

Browser → GCP HTTPS load balancer → consumer/admin Services → shared Caddy/Next.js Pod → isolated consumer/operator APIs.

| Route | Service / Pod port | Authentication |
| --- | --- | --- |
| `/admin`, `/admin/*` | `operator-frontend` / Caddy `8082` | Configured IAP access; operator API verifies the assertion. |
| Consumer pages, `/v1/*`, `/auth/*`, `/_next/static/*` | `frontend` / Caddy `8080` | Guest or consumer account. |
| Health checks | Both backend health policies / `8081` | No browser route; consumer readiness only. Verify operator API readiness separately. |

Next.js `3000` accepts only Pod-local proxy traffic. `/administrator` is not an admin route. HTTP redirects preserve path/query, including IAP's return to `/admin?gcp-iap-mode=AUTHENTICATING`.

| Owner | Resources |
| --- | --- |
| Shared platform | Private GKE/VPC, standard Gateway controller and HTTP load-balancing dependency. |
| [Public-access Terraform](../infra/terraform/environments/public-access) | `ec-public-ip`, one DNS authorization, managed certificate/map entry, TLS policy and empty `ec-admin-iap` secret container. |
| Application Helm | Gateway, exact-host HTTPRoutes, backend/health policies, shared web Deployment and separate API processes. |
| [Consumer identity](../deployment/consumer-identity.md) | Identity Platform, restricted browser key and backend-scoped IAP grant and private RBAC configuration. |
| DNS authority | Certificate-validation CNAME and one A record; no tunnel or HTTP proxy. |

- Gateway class: `gke-l7-global-external-managed`. No proxy-only subnet, public node or public Kubernetes endpoint.
- Certificate Manager uses `ec-public-cert-map`; do not combine its annotation with Gateway TLS Secret references. [Certificate configuration](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway).
- `ec-public-tls` requires TLS 1.2 or newer. Google manages public certificate renewal while the DNS authorizations remain valid.
- Caddy matches original paths before normalization. Consumer `8080` rejects all admin paths, dot segments, encoded separators and double encoding. Only safe encoded path segments and query data pass. `/_next/image` and `/_next/data` are excluded; immutable build assets are public.
- Admin `8082` accepts only exact-host `/admin` paths and one bounded JWT-shaped assertion. This checks presence/format, not signature. Missing/duplicate assertions fail; the operator API verifies the JWT. Forwarding authority is fixed; unsigned identity and middleware headers are removed.
- Shared Next.js uses consumer `EC_API_ORIGIN` and separate `EC_OPERATOR_API_ORIGIN`. No database, Google OAuth or IAP secret is mounted. Consumer proxy cookies are limited to `__Host-ec_login`, `__Host-ec_session` and `__Host-ec_csrf`; IAP cookies never reach consumer FastAPI.
- Consumer and IAP sessions are independent; consumer logout does not log out IAP. Both views share the browser origin and web process: a consumer XSS could act as a signed-in admin. Exact-Origin CSRF does not isolate scripts on the same origin; API processes and credentials remain isolated.
- Google Front End ranges reach only reviewed frontend/health ports. NetworkPolicy keeps API/store access scoped; review controller-created firewall rules and NEG endpoints.
- IAP protects only the admin frontend backend. The API verifies the signed assertion independently: Google issuer/keys, backend audience, timestamps, signed identity and its configured RBAC binding.
- Load balancer → filter/frontend uses HTTP within the restricted VPC/Pod network. This is not application mTLS. [Datastore TLS/mTLS](development.md#authenticated-discovery-and-encrypted-dependencies) remains a separate release gate.
- Single-node hosting has no node-level HA. [Load-balancer rules, processing and internet egress](https://cloud.google.com/vpc/network-pricing#lb) add cost; a reserved IP is charged while unused. No new application node is required.

## Prepare

1. Verify CLI account/project, approved commits, immutable images and current state. Target `iz27-platform-dev`; preserve existing data, Symphony OAuth configuration and unrelated DNS.
2. Review the platform plan: only enable HTTP load balancing and `CHANNEL_STANDARD` on the existing private cluster. Apply under explicit cloud authorization, then verify the GatewayClass/controller.
3. Configure the [public-access backend](../infra/terraform/environments/public-access/backend.tf.example) in the existing protected application bucket. Review the saved plan before applying. This root must not adopt cluster/network/IAP API state, OAuth payloads or a controller-managed load balancer.
4. Add only the output certificate-validation CNAME at the current authoritative DNS provider, DNS-only. Wait for authorization and the managed certificate to become active. This record does not route application traffic.
5. Prepare the separate admin OAuth client below. Provision only its versioned namespace Secret through the approved secret channel; no secret value in Helm/Terraform, logs or command arguments.
6. Complete [private authenticated rollout](development.md#authenticated-discovery-and-encrypted-dependencies): recovery-key custody, held current backup, coordinated store TLS/Temporal mTLS, migrations through `0208`, consumer identity and private readiness. Preserve rollback and capacity limits.

Offline prerequisites:

```sh
terraform -chdir=infra/terraform/environments/public-access init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/terraform/environments/public-access validate
terraform -chdir=infra/terraform/environments/public-access test -no-color
```

## Admin

- Use a dedicated **External Web OAuth client** for IAP; personal Gmail cannot use an organization-only Google-managed client. Keep it separate from the consumer Firebase callback/client. [Custom IAP OAuth](https://docs.cloud.google.com/iap/docs/custom-oauth-configuration).
- Register `https://iap.googleapis.com/v1/oauth/clientIds/<admin-client-id>:handleRedirect`. Preserve the project's existing consent brand and other clients.
- Import the approved credential into a pinned `ec-admin-iap` Secret Manager version. The same-namespace Secret referenced by `operator.iapClientSecretName` supplies the credential in data key `key`; application containers never mount it. Google's [Gateway-specific sample](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-gateway-resources?hl=es#configure_iap) documents this format. Require successful policy attachment and backend IAP readback before activation.
- Supply the approved backend member privately through Terraform `operator_iap_member`; grant `roles/iap.httpsResourceAccessor` only on the actual **operator frontend backend service**. Inspect inherited permissions; broader grants invalidate the reviewed access boundary.
- Set `operator.iapAudience=/projects/<project-number>/global/backendServices/<operator-frontend-backend-id>` from the created backend. Neither the API ID nor consumer client/backend ID is valid.
- Publish the private [RBAC policy](operator-access.md#publish-and-activate) and configure its explicit Parameter Manager version. Match verified subject and signed email; never substitute an unsigned header or test fixture.
- Disable the pinned policy version to deny API access after its bounded cache expires, or publish and roll out revised grants. Revoke edge sessions/grants as appropriate; signature validation alone does not establish immediate online session revocation.

## Activate

1. Review the complete Helm resources, IP/certificate/IAP configuration and private acceptance before approving the public Gateway. Creating an external load balancer can expose traffic **before DNS is published**; an empty A record is not an access boundary.
2. Layer [identity values](helm/events-concierge/values-consumer-identity.example.yaml) and [public edge values](helm/events-concierge/values-public-edge.example.yaml) over the approved private release. Set `publicEdge.enabled=true`, `bootstrap=true`, `staticIpName=ec-public-ip`, `certificateMap=ec-public-cert-map`, `sslPolicy=ec-public-tls`; legacy `gateway.enabled=false`.
3. Bootstrap uses an explicitly nonmatching admin Service selector, disabled operator API/BFF, no admin Caddy handler and no GFE ingress on `8082`. Keep audience/policy assignments empty and the separate `operator-frontend` workload disabled. Use the real admin client/Secret; never deploy fixture identities.
4. Verify programmed Gateway/routes, certificate and healthy consumer backend. Read back the actual admin backend IAP policy and approved IAM grant; configure its real audience and private RBAC version. Only then attest `operatorAccessVerified=true`, set `bootstrap=false` and enable `operator`/`operator-api`. The attestation records a completed human preflight; Helm does not query cloud permissions. The protected Service now selects the shared frontend on `8082`; the separate operator frontend stays disabled.
5. Require both APIs ready, then run `wait_ready.py --target shared --profile private --operator --shared-frontend`. Check actual NEGs/firewalls, backend IAP, direct-IP/unexpected-Host rejection and consumer exclusions before publishing the output A record, DNS-only. Preserve unrelated DNS.
6. Verify HTTP → HTTPS preserves host/path/query and trusted certificate validation. In Chrome test anonymous browsing/filters, Google cancellation/signup/login/logout, saved filters and the configured legal mode. Verify cookies, CSRF, two-account isolation and same-account erasure.
7. Open `/admin` and verify IAP administrator login and its OAuth return on the same path. Verify reads/commands, consumer and admin sessions, CSRF and unassigned-user rejection. Missing/forged/duplicate/expired/wrong-audience assertions fail. On consumer `8080`, all `/admin` paths return `404`; Gateway sends them only to the protected backend. Probe paths, optimizer/data aliases and unknown hosts fail; normalized aliases cannot reach admin content.
8. Promote the accepted canary, then resume cadence last through the private rollout procedure. Browser acceptance is required before declaring release completion.

Failed or unavailable acceptance: remove the dedicated public HTTPRoutes/Gateway exposure first,
then withdraw only the app A record. DNS removal alone does not stop direct-IP traffic.
Preserve identities, databases, recovery copies and private operator access.

## Operate

- Inspect Gateway/HTTPRoute conditions, backend health, IAP denials and HTTP status/latency in [Load balancing](https://console.cloud.google.com/net-services/loadbalancing/list/loadBalancers?project=iz27-platform-dev&authuser=4).
- Monitor certificate state/expiry and DNS authorizations in [Certificate Manager](https://console.cloud.google.com/security/ccm/list/certificates?project=iz27-platform-dev&authuser=4). Public certificate renewal is independent of private datastore CA rotation.
- Keep request/header/token debug logs disabled. Do not read or copy IAP secrets from backend-service output.
- Preserve controller ownership of NEGs/load-balancer resources; do not patch them manually. Use reviewed Kubernetes policies for changes.
- Rotate admin OAuth through a new pinned secret version and namespace Secret, verify IAP before retiring the old credential.
- Roll back only to a tested schema-compatible image/configuration. Preserve data and recovery assets; never fall back to mock data or another product's origin.
