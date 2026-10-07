# Application access

Tools, target verification and private application connections. [Platform access](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/access.md) owns the IAP tunnel and dedicated kubeconfig. Consumer HTTPS/IAP admin routes are defined in [public access](public-access.md).

## Connect

- Tools: Python 3.12, gcloud, `gke-gcloud-auth-plugin`, kubectl, Helm 3, Terraform 1.16.1 and Docker Buildx.
- Dependencies: `uv sync --frozen --python 3.12`; use `.venv/bin/python` for operation helpers.
- Set `APP_GCP_ACCOUNT` privately to the approved deployment account; verify it and the explicit cloud project. Recovery helpers also enforce their approved account.
- [Platform access](https://github.com/iliazlobin/gcp-foundation/blob/faf282757842f4c5830c39ec721d2887c9a7e59e/docs/access.md): keep IAP running; export its dedicated kubeconfig.
- Required context: `gke_iz27-platform-dev_us-west1-a_platform-dev`; Ready nodes.

```bash
kubectl config current-context
kubectl get nodes
```

## Loopback access

Supervised Mac access may manage IAP, consumer and admin forwards. These loopback links work only on the forwarding machine; confirm their current ownership in the release record. Authenticated consumer/admin access follows the [public edge](public-access.md); another machine needs its own platform authentication for private operations.

Without supervised access, keep IAP running and use one forward per terminal:

```bash
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-api 14000:8000
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 service/events-concierge-frontend 14001:80
kubectl -n events-concierge-dev port-forward --address=127.0.0.1 deployment/events-concierge-admin 14002:3000
```

- [Consumer](http://127.0.0.1:14001) · [Admin](http://127.0.0.1:14002/admin); dedicated shared kubeconfig.
- Tunnel interruption: restart platform access, verify context/nodes, restart forwards.
- Pod replacement: restart affected forwards; Service forwards stay attached to the selected pod.

## Private HTTPS preparation

- Origin: `https://localhost:14443`; callback: `https://localhost:14443/auth/callback`.
- `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`; real identity, encrypted dependencies and restricted credentials still required.
- Keep frontend forward running; browser-trusted localhost certificate and protected key outside Git.
- Set absolute `EC_LOCAL_TLS_CERT` / `EC_LOCAL_TLS_KEY` paths; never bypass certificate warnings.

```bash
caddy validate --config deploy/private-access.Caddyfile --adapter caddyfile
caddy run --config deploy/private-access.Caddyfile --adapter caddyfile
```

- [Proxy](../../deploy/private-access.Caddyfile): loopback `127.0.0.1:14443`; preserves Host; admin listener, redirects and automatic trust installation disabled.
- No public DNS/ingress/firewall opening. Browser TLS does not encrypt datastores.
- [Google activation and deployed login/CSRF/logout checks](consumer-identity.md#built-in-oidc-bff-activation).

## Demo admin

- `events-concierge-admin`: frontend/API on pod loopback; no Service; Kubernetes port-forward permission required.
- Ordinary API: administration disabled. Cadence uses separate CronJob.
- Shared live data and immutable images; readiness checks admin overview through API/frontend.
- [Open via admin forward](#loopback-access).
- Authenticated public releases use the shared frontend `/admin`, separate operator API and [configured IAP/RBAC](operator-access.md). The demo Deployment is absent from the private authenticated profile.
