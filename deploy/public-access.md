# Public access

Public origin: **https://events.iliazlobin.com**. The Helm connector is opt-in;
publishing DNS requires an approved non-mock release, private readiness checks and configured
[consumer identity](../deployment/consumer-identity.md). The private development profile
cannot enable it. The [authenticated datastore composition](development.md#authenticated-discovery-and-encrypted-dependencies)
must be released first; this package does not convert plaintext stores to TLS.
Admin origin: **https://admin-events.iliazlobin.com**, separately protected by Cloudflare Access
when the operator's `cloudflare_access` mode is enabled. Consumer browsing remains anonymous;
consumer login grants no operator role. Configuration/rendering does not establish deployed access.

## Route and ownership

Browser → Cloudflare HTTPS → encrypted tunnel → GKE consumer filter → consumer frontend → API.

- Create a dedicated **Events Concierge** remotely managed tunnel in the
  [Cloudflare account](https://dash.cloudflare.com/cbd93b4cd44ff28c000239e7da2e5cbc/tunnels).
  Keep the Mac's Hermes tunnel and wildcard route intact. Every new replica must reach
  the same origin; [Kubernetes connectors](https://developers.cloudflare.com/tunnel/guides/kubernetes/)
  run inside GKE independently of the Mac.
- Two small connector pods run digest-pinned cloudflared and Caddy. The filter admits
  the exact consumer hostname and consumer routes; admin, probes and unknown paths return
  `404`. It removes forwarded operator identity headers. Cloudflare admin uses a separate
  loopback listener and operator frontend; existing IAP deployments keep their default mode.
- The connector has no Kubernetes API token, GCP identity or application credentials.
  Its only secret is a tunnel-specific token file. NetworkPolicy permits DNS, the consumer
  frontend, the operator frontend only when Cloudflare admin is enabled, and
  [Cloudflare tunnel endpoints](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
  on TCP/UDP 7844. DNS permits only `kube-dns` and [NodeLocal DNSCache](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nodelocal-dns-cache)
  pods in `kube-system`, port 53. No public GCP Gateway, LoadBalancer or inbound firewall rule is added.
- The data chart must exclude `public-tunnel` from its internal allow policy in both directions.
  Deploy that change with the authenticated profile; overlapping allow policies would otherwise
  bypass the connector's intended network limits.
- Cloudflare terminates browser TLS; the tunnel is encrypted. The final filter-to-frontend
  leg is HTTP restricted by NetworkPolicy inside the namespace. This is not end-to-end
  application mTLS. Database, Redis and Temporal encryption remain independent release gates.
- Two replicas share the existing node capacity; one-node hosting has no node-level HA.
  Combined requests: 100m CPU / 256 MiB. NAT traffic and existing GKE capacity still incur cost.

## Activate

1. Verify the approved image/configuration, datastore TLS/mTLS, private guest catalog,
   Google provider configuration, approved legal mode and owner-only Access policy.
   Confirm GKE enforces NetworkPolicy. Real Google/Access browser checks require the published HTTPS origins.
2. Create the dedicated tunnel **without a published route**. Store its token through the
   approved secret channel in a versioned, immutable Kubernetes Secret such as
   `ec-cloudflare-tunnel-v1`, key `token`. Never paste credentials into Git, reports,
   process arguments or Helm values. [cloudflared reads a token file](https://developers.cloudflare.com/tunnel/reference/run-parameters/#token-file).
3. Layer [identity values](helm/events-concierge/values-consumer-identity.example.yaml) and
   [tunnel values](helm/events-concierge/values-public-tunnel.example.yaml) over the exact
   approved release values. Render/lint, review the resources, then deploy under release
   authorization. Require frontend readiness, both connector containers ready and healthy
   Cloudflare connections before publishing. No token value belongs in the chart.
4. Configure [host-specific HTTP redirects](https://developers.cloudflare.com/rules/url-forwarding/examples/redirect-admin-https/):
   `http://events.iliazlobin.com/*` → `https://events.iliazlobin.com/${1}` and
   `http://admin-events.iliazlobin.com/*` → `https://admin-events.iliazlobin.com/${1}`.
   Use status `308` and **Preserve query string**. Keep other hosts' rules unchanged.
   Then publish only the approved tunnel route: hostname `events.iliazlobin.com`,
   HTTP service `http://127.0.0.1:8080`, original Host preserved. No wildcard, alternate
   origin or Host override. [Creating the route adds DNS](https://developers.cloudflare.com/tunnel/concepts/routing/):
   proxied CNAME `events` → `<dedicated-tunnel-uuid>.cfargotunnel.com`, TTL Auto.
   Check the existing record before creation; publication makes consumer access public.
5. Verify HTTP redirects preserve path/query, trusted public TLS and guest browsing in Chrome,
   filtered URLs, Google cancellation/signup/
   login/logout, the configured legal behavior and saved filters. Request `/admin`, `/admin/v1/ingestion`,
   `/metrics` and `/readyz`: each must return `404`. Confirm private admin still works.
   Verify cookies, CSRF, two-account isolation and tenant erasure on the deployed origin.

Publication permits browser acceptance; it does not complete the release. If acceptance fails
or cannot be completed, disable only the dedicated Events Concierge published routes.
Preserve identities, data, recovery copies and the existing private operator route.

## Cloudflare admin

Browser → owner-only Access application → dedicated tunnel `:8082` → operator frontend → operator API.

1. In the existing Zero Trust team, create a self-hosted Access application for the complete
   `admin-events.iliazlobin.com` hostname. Allow only `iliazlobin91@gmail.com` through the approved
   identity provider; do not add a Bypass or Service Auth policy. Leave `events.iliazlobin.com`
   outside this application. Require application tokens to live at most one hour, including
   [policy/global/Cloudflare One Client session overrides](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/).
2. Verify the existing team issuer `https://wild-butterfly-a721.cloudflareaccess.com`, the actual
   application's AUD, and the owner's stable signed `sub`. Layer
   [Cloudflare admin values](helm/events-concierge/values-cloudflare-admin.example.yaml) over the
   reviewed private runtime, tunnel and isolated operator credentials. Empty AUD/subject values
   deliberately block rendering. Remove IAP/TLS Gateway fields when selecting Cloudflare mode.
3. Resolve the team's `/cdn-cgi/access/certs` endpoint and approve only the containing
   [Cloudflare HTTPS CIDRs](https://www.cloudflare.com/ips-v4) in `operator.cloudflareAccess.jwksCidrs`.
   Kubernetes NetworkPolicy cannot select a hostname; review DNS changes if verification fails.
   The operator API permits these CIDRs on 443, its application PostgreSQL, DNS and ADC; it has
   no consumer, Redis or Temporal access. The connector cannot reach the operator API directly.
4. After approved deployment/readiness, add the exact admin hostname to the dedicated Events
   tunnel with service `http://127.0.0.1:8082`, original Host preserved. Retain the consumer route
   on `:8080` and the final `http_status:404` rule. Never use wildcard routes or the Hermes tunnel.
5. Verify owner login and admin reads/commands in Chrome. Non-owners, missing/forged assertions,
   wrong issuer/AUD, expired tokens, duplicate assertions and cross-origin mutations must fail.
   Consumer `/admin` and both hosts' public probes remain `404`; guest catalog access must work.

The API independently verifies the [Access application JWT](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/):
RS256, fixed team issuer/certificate endpoint, exact single application AUD, timestamps,
`type=app`, signed owner email and configured subject/role. Unsigned email headers, consumer
cookies and service tokens grant no authority. Signing keys refresh through bounded JWKS
resolution. Removing/re-adding an Access user changes `sub` and requires a reviewed allowlist
update. Local signature/expiry checks do not perform an online session-revocation lookup;
revoke the Access session at the edge and deploy removal of the subject assignment. A retained
token remains valid to the origin until expiry if the origin still has the old allowlist.

## Operate

- Inspect `events-concierge-public-tunnel` Deployment, readiness and PodMonitoring. `/ready`
  reports Cloudflare connectivity; the filter's private readiness checks the frontend.
  Pod readiness alone does not prove public routing or Google acceptance.
- Failed connections reconnect without a liveness restart loop. A frontend outage returns
  an error; the filter never falls back to admin, a demo or another origin. Keep request/
  header debug logging disabled. Check HTTP status metrics and application logs.
- Token rotation: provision a new immutable Secret, update `tokenSecretName`, verify both
  new connections, then revoke the old tunnel credential. The proxy never mounts the token.
- Rollback: remove/disable this dedicated consumer and optional admin published route first,
  then disable `publicTunnel` and the Cloudflare operator profile together.
  Preserve user identities/data and the private operator route. Never retarget DNS to Hermes
  or restore an incompatible mock/schema release to recover a public endpoint.
- Offline verification: `EC_HELM_BINARY=helm EC_PUBLIC_TUNNEL_DOCKER=1 .venv/bin/python -m pytest tests/unit/test_public_tunnel.py -o addopts= -q`.
  This rehearsal uses synthetic data, no tunnel token and a network-isolated container;
  [deployment CI](../.github/workflows/deployment-validation.yml) runs it too.
