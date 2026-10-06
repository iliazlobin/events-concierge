# Public access

- Consumer: **https://events.iliazlobin.com**; anonymous catalog browsing, Google accounts, chat disabled.
- Admin: **https://admin-events.iliazlobin.com/admin**; Google IAP allows only `iliazlobin91@gmail.com`.
- Consumer `/admin` and `/admin/` accept browser GET/HEAD navigation only: `302` to the fixed admin URL, without query parameters. Admin APIs remain on the protected hostname.
- One global external Application Load Balancer, reserved IPv4 and managed certificate cover both hosts.
- GKE nodes, control plane, API and databases stay private. Consumer sign-in grants no admin role.
- Rendering, healthy Pods and green CI do not establish public or browser acceptance.

## Route and ownership

Browser → GCP HTTPS load balancer → separate consumer/admin Services → private APIs.

| Owner | Resources |
| --- | --- |
| Shared platform | Private GKE/VPC, standard Gateway controller and HTTP load-balancing dependency. |
| [Public-access Terraform](../infra/terraform/environments/public-access) | `ec-public-ip`, two DNS authorizations, managed certificate/map, TLS policy and empty `ec-admin-iap` secret container. |
| Application Helm | Gateway, exact-host HTTPRoutes, backend/health policies, consumer filter and isolated operator workloads. |
| [Consumer identity](../deployment/consumer-identity.md) | Identity Platform, restricted browser key and optional backend-scoped owner IAP grant. |
| DNS authority | Certificate-validation CNAMEs and two A records; no tunnel or HTTP proxy. |

- Gateway class: `gke-l7-global-external-managed`. No proxy-only subnet, public node or public Kubernetes endpoint.
- Certificate Manager uses `ec-public-cert-map`; do not combine its annotation with Gateway TLS Secret references. [Certificate configuration](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway).
- `ec-public-tls` requires TLS 1.2 or newer. Google manages public certificate renewal while the DNS authorizations remain valid.
- Consumer Caddy runs beside the existing frontend. Only exact raw `/admin` or `/admin/` GET/HEAD requests redirect; admin pages/APIs are never proxied. Admin API/probe routes, other entry methods and unknown hosts return `404`. Paths that normalize to an allowed consumer route can serve that public page, such as `/admin/..` → `/`. Consumer requests have operator identity headers removed.
- Google Front End ranges reach only reviewed frontend/health ports. NetworkPolicy keeps API/store access scoped; review controller-created firewall rules and NEG endpoints.
- IAP protects only the admin frontend backend. The API verifies the signed assertion independently: Google issuer/keys, backend audience, timestamps, signed owner email and explicit subject role.
- Load balancer → filter/frontend uses HTTP within the restricted VPC/Pod network. This is not application mTLS. [Datastore TLS/mTLS](development.md#authenticated-discovery-and-encrypted-dependencies) remains a separate release gate.
- Single-node hosting has no node-level HA. [Load-balancer rules, processing and internet egress](https://cloud.google.com/vpc/network-pricing#lb) add cost; a reserved IP is charged while unused. No new application node is required.

## Prepare

1. Verify CLI account/project, approved commits, immutable images and current state. Target `iz27-platform-dev`; preserve existing data, Symphony OAuth configuration and unrelated DNS.
2. Review the platform plan: only enable HTTP load balancing and `CHANNEL_STANDARD` on the existing private cluster. Apply under explicit cloud authorization, then verify the GatewayClass/controller.
3. Configure the [public-access backend](../infra/terraform/environments/public-access/backend.tf.example) in the existing protected application bucket. Review the saved plan before applying. This root must not adopt cluster/network/IAP API state, OAuth payloads or a controller-managed load balancer.
4. Add only the output certificate-validation CNAMEs at the current authoritative DNS provider, DNS-only. Wait for both authorizations and the managed certificate to become active. These records do not route application traffic.
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
- Grant `roles/iap.httpsResourceAccessor` only to `user:iliazlobin91@gmail.com` on the actual **operator frontend backend service**. Inspect inherited permissions; broader grants invalidate owner-only edge acceptance.
- Set `operator.iapAudience=/projects/<project-number>/global/backendServices/<operator-frontend-backend-id>` from the created backend. Neither the API ID nor consumer client/backend ID is valid.
- Record the owner's verified `accounts.google.com:<subject>` as the sole `reviewer` assignment. Never substitute the former Cloudflare UUID, an email header or a test fixture.
- Removing the subject assignment blocks API access. Revoke edge sessions/grants as appropriate; origin signature validation alone does not establish immediate online session revocation.

## Activate

1. Review the complete Helm resources, IP/certificate/IAP configuration and private acceptance before approving the public Gateway. Creating an external load balancer can expose traffic **before DNS is published**; an empty A record is not an access boundary.
2. Layer [identity values](helm/events-concierge/values-consumer-identity.example.yaml) and [public edge values](helm/events-concierge/values-public-edge.example.yaml) over the approved private release. Set `publicEdge.enabled=true`, `bootstrap=true`, `staticIpName=ec-public-ip`, `certificateMap=ec-public-cert-map`, `sslPolicy=ec-public-tls`; legacy `gateway.enabled=false`.
3. Bootstrap renders the Gateway and admin Service/IAP policy with no operator endpoints. Keep operator workloads disabled and audience/subject assignments empty. Use the real separate admin client/Secret; never deploy fixture identity values.
4. Verify Gateway/HTTPRoutes and policies are accepted/programmed, static IP and certificate match, and the consumer backend is healthy. Resolve the generated admin backend ID, apply the exact owner binding and configure its real audience/verified subject. Set `bootstrap=false` and enable the isolated operator pair.
5. Verify IAP is enabled on admin and absent on consumer. Check actual NEGs/firewalls, backend health, direct-IP/unexpected-Host rejection and consumer exclusions before publishing the two output A records, DNS-only. Preserve other hostnames/nameservers; remove conflicting records only after inspecting their ownership.
6. Verify HTTP → HTTPS preserves host/path/query and trusted certificate validation. In Chrome test anonymous browsing/filters, Google cancellation/signup/login/logout, saved filters and the configured legal mode. Verify cookies, CSRF, two-account isolation and same-account erasure.
7. Open consumer `/admin` and verify the fixed HTTPS redirect reaches IAP and the console after owner login. Verify admin reads/commands; non-owners, consumer tokens, missing/forged/duplicate/expired/wrong-audience assertions and cross-origin mutations fail. Consumer `/admin/v1/*`, non-navigation entry methods, `/metrics`, `/readyz` and unknown hosts return `404`; normalized aliases cannot reach admin content. Verify the IAP OAuth return; the admin root is retained for the callback.
8. Promote the accepted canary, then resume cadence last through the private rollout procedure. Browser acceptance is required before declaring release completion.

Failed or unavailable acceptance: remove the dedicated public HTTPRoutes/Gateway exposure first,
then withdraw only the two app A records. DNS removal alone does not stop direct-IP traffic.
Preserve identities, databases, recovery copies and private operator access.

## Operate

- Inspect Gateway/HTTPRoute conditions, backend health, IAP denials and HTTP status/latency in [Load balancing](https://console.cloud.google.com/net-services/loadbalancing/list/loadBalancers?project=iz27-platform-dev&authuser=4).
- Monitor certificate state/expiry and DNS authorizations in [Certificate Manager](https://console.cloud.google.com/security/ccm/list/certificates?project=iz27-platform-dev&authuser=4). Public certificate renewal is independent of private datastore CA rotation.
- Keep request/header/token debug logs disabled. Do not read or copy IAP secrets from backend-service output.
- Preserve controller ownership of NEGs/load-balancer resources; do not patch them manually. Use reviewed Kubernetes policies for changes.
- Rotate admin OAuth through a new pinned secret version and namespace Secret, verify IAP before retiring the old credential.
- Roll back only to a tested schema-compatible image/configuration. Preserve data and recovery assets; never fall back to mock data or another product's origin.
