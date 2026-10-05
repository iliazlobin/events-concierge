# Public consumer access

Public origin: **https://events.iliazlobin.com**. The Helm connector is opt-in;
publishing DNS requires a reviewed, verified non-mock release and configured
[consumer identity](../deployment/consumer-identity.md). The private development profile
cannot enable it. The [authenticated datastore composition](development.md#remaining-release-work)
must be released first; this package does not convert plaintext stores to TLS.

## Route and ownership

Browser → Cloudflare HTTPS → encrypted tunnel → GKE consumer filter → consumer frontend → API.

- Create a dedicated **Events Concierge** remotely managed tunnel in the
  [Cloudflare account](https://dash.cloudflare.com/cbd93b4cd44ff28c000239e7da2e5cbc/tunnels).
  Keep the Mac's Hermes tunnel and wildcard route intact. Every new replica must reach
  the same origin; [Kubernetes connectors](https://developers.cloudflare.com/tunnel/guides/kubernetes/)
  run inside GKE independently of the Mac.
- Two small connector pods run digest-pinned cloudflared and Caddy. The filter admits
  the exact consumer hostname and consumer routes; admin, probes and unknown paths return
  `404`. It removes forwarded operator identity headers. Admin retains its private/IAP route.
- The connector has no Kubernetes API token, GCP identity or application credentials.
  Its only secret is a tunnel-specific token file. NetworkPolicy permits DNS, the consumer
  frontend and [Cloudflare tunnel endpoints](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
  on TCP/UDP 7844. DNS permits only `kube-dns` and [NodeLocal DNSCache](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nodelocal-dns-cache)
  pods in `kube-system`, port 53. No public GCP Gateway, LoadBalancer or inbound firewall rule is added.
- Cloudflare terminates browser TLS; the tunnel is encrypted. The final filter-to-frontend
  leg is HTTP restricted by NetworkPolicy inside the namespace. This is not end-to-end
  application mTLS. Database, Redis and Temporal encryption remain independent release gates.
- Two replicas share the existing node capacity; one-node hosting has no node-level HA.
  Combined requests: 100m CPU / 256 MiB. NAT traffic and existing GKE capacity still incur cost.

## Activate

1. Verify the approved application image/configuration, datastore TLS/mTLS, guest catalog,
   Google login, consent and owner-only admin. Confirm GKE actually enforces NetworkPolicy.
2. Create the dedicated tunnel **without a published route**. Store its token through the
   approved secret channel in a versioned, immutable Kubernetes Secret such as
   `ec-cloudflare-tunnel-v1`, key `token`. Never paste credentials into Git, reports,
   process arguments or Helm values. [cloudflared reads a token file](https://developers.cloudflare.com/tunnel/reference/run-parameters/#token-file).
3. Layer [identity values](helm/events-concierge/values-consumer-identity.example.yaml) and
   [tunnel values](helm/events-concierge/values-public-tunnel.example.yaml) over the exact
   approved release values. Render/lint, review the resources, then deploy under release
   authorization. Require frontend readiness, both connector containers ready and healthy
   Cloudflare connections before publishing. No token value belongs in the chart.
4. Add only this tunnel's published application route: hostname `events.iliazlobin.com`,
   HTTP service `http://127.0.0.1:8080`, original Host preserved. No wildcard, alternate
   origin or Host override. [Creating the route adds DNS](https://developers.cloudflare.com/tunnel/concepts/routing/):
   proxied CNAME `events` → `<dedicated-tunnel-uuid>.cfargotunnel.com`, TTL Auto.
   Check the existing record before creation; publication makes consumer access public.
5. Verify public TLS and guest browsing in Chrome, filtered URLs, Google cancellation/signup/
   login/logout, legal acceptance and saved filters. Request `/admin`, `/admin/v1/ingestion`,
   `/metrics` and `/readyz`: each must return `404`. Confirm private admin still works.
   Verify cookies, CSRF, two-account isolation and tenant erasure on the deployed origin.

## Operate

- Inspect `events-concierge-public-tunnel` Deployment, readiness and PodMonitoring. `/ready`
  reports Cloudflare connectivity; the filter's private readiness checks the frontend.
  Pod readiness alone does not prove public routing or Google acceptance.
- Failed connections reconnect without a liveness restart loop. A frontend outage returns
  an error; the filter never falls back to admin, a demo or another origin. Keep request/
  header debug logging disabled. Check HTTP status metrics and application logs.
- Token rotation: provision a new immutable Secret, update `tokenSecretName`, verify both
  new connections, then revoke the old tunnel credential. The proxy never mounts the token.
- Rollback: remove/disable this dedicated published route first, then disable `publicTunnel`.
  Preserve user identities/data and the private operator route. Never retarget DNS to Hermes
  or restore an incompatible mock/schema release to recover a public endpoint.
- Offline verification: `EC_HELM_BINARY=helm EC_PUBLIC_TUNNEL_DOCKER=1 .venv/bin/python -m pytest tests/unit/test_public_tunnel.py -o addopts= -q`.
  This rehearsal uses synthetic data, no tunnel token and a network-isolated container;
  [deployment CI](../.github/workflows/deployment-validation.yml) runs it too.
