# Consumer accounts

Google/Apple signup uses GCP Identity Platform. Browse the published catalog without
an account; sign in and accept the current legal documents before storing personal data.
Implementation is available; provider setup and deployed acceptance remain release gates.

## Access

| Caller | Allowed |
| --- | --- |
| Guest | Events, Map, Calendar, Entities/Graph, event details and provider links. |
| Signed-in user | Own profile, preferences, saved filters and account erasure. PostgreSQL RLS and CSRF protect writes. |
| Admin | Separate IAP operator surface: signed email `iliazlobin91@gmail.com` **and** the configured stable subject/role. Consumer login grants no admin role. |

- Publish approved, versioned HTTPS Terms of Service and Privacy Policy pages. The checkbox starts unchecked; the API records account binding and acceptance atomically.
- Accounts bind the verified Identity Platform project/UID, never email. Different provider identities stay separate; account linking is not implemented. Apple private-relay addresses are supported.
- ID tokens exist briefly in SDK memory. The API verifies issuer, audience, provider, verified email, freshness and revocation using the [Admin SDK](https://firebase.google.com/docs/auth/admin/verify-id-tokens).
- The browser receives an opaque Secure/HttpOnly session and CSRF cookie. Default session: eight hours; managed disable/revocation checks cache for at most 60 seconds, with no stale fallback.
- Erasure requires a fresh, same-account provider login. The erasure worker fences sessions, removes the managed identity and completes application cleanup; failed identity deletion keeps erasure incomplete.
- Changing legal content requires a new version and immutable URL. Existing accounts must accept it again. Acceptance receipts are tenant-scoped and removed during account erasure.

## GCP setup

[Terraform root](../infra/terraform/environments/consumer-identity) owns Identity Platform,
the restricted browser key, provider secret containers and narrow per-process IAM.
It adds no GKE nodes or database. Use existing application-project service accounts and
the protected application state bucket; never adopt shared foundation/network/cluster state.

| Resource | Configuration |
| --- | --- |
| Identity Platform | Google/Apple only; anonymous/password/phone signup and automatic email linking disabled. Signup starts disabled. |
| Browser API key | Exact application/auth domains; Identity Toolkit and Secure Token APIs only. Public identifier, not user authority. |
| API Workload Identity | `firebaseauth.users.get`; no provider secrets or identity deletion. |
| Erasure Workload Identity | `firebaseauth.users.get` and `firebaseauth.users.delete`. |
| Provider secrets | `ec-consumer-google`, `ec-consumer-apple`; numbered Secret Manager versions, operator access only. Values never enter Terraform state or containers. |
| Operator edge | Existing IAP backend; optional owner grant. The API separately enforces the signed owner email and subject. |

1. Confirm the production HTTPS origin and operator host/audience. Consumer ingress supports guests; admin ingress remains IAP-protected. Verify the active CLI account independently of the [Identity console](https://console.cloud.google.com/customer-identity/providers?project=iz27-platform-dev&authuser=4).
2. Prepare the [backend example](../infra/terraform/environments/consumer-identity/backend.tf.example) and [variables](../infra/terraform/environments/consumer-identity/identity.tfvars.example). Initialize this new app-owned state, review a saved plan, then apply only after deployment authorization.
3. Configure provider credentials below and publish approved legal pages. Keep `signup_enabled=false` until the deployed account flow can be tested in a bounded pilot.

**Private Google pilot:** in `iz27-platform-dev`, set `public_origin_profile="private_loopback_https"`
and `public_origin="https://localhost:14443"`. Identity Platform authorizes `localhost`; the browser
key permits only that exact HTTPS port and the project auth domain. Use the existing
[private HTTPS proxy](../deploy/development.md#shared-release-and-access), a browser-trusted
certificate and `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`. Keep public ingress closed.
Configure `EC_IDENTITY_PLATFORM_PROVIDERS='["google.com"]'`; Apple activation can follow separately.
Guests see **Sign in**; the same Google flow signs in existing users or creates a new account.
The selected catalog view and filters survive sign-in. Personal settings require an account.

```sh
terraform -chdir=infra/terraform/environments/consumer-identity init -backend-config=backend.hcl
terraform -chdir=infra/terraform/environments/consumer-identity plan -var-file=identity.tfvars -out=identity.tfplan
# After reviewing this exact plan and receiving deployment authorization:
terraform -chdir=infra/terraform/environments/consumer-identity apply identity.tfplan
```

**Google:** use a dedicated web OAuth client with the approved application origin and
`https://<project-id>.firebaseapp.com/__/auth/handler` callback. Secret JSON fields:
`client_id`, `client_secret`.
The [OAuth consent brand](https://support.google.com/cloud/answer/15549049) belongs to its
Google project; a second client does not give it a separate app name. Keep other products'
existing clients and branding intact when choosing the Events Concierge identity project.

**Apple:** requires Apple Developer membership, a Sign in with Apple-enabled app,
Services ID, Team ID, Key ID and private key. Register the same auth-domain callback
and review private-relay email requirements. Secret JSON fields: `client_id` (Services ID),
`team_id`, `key_id`, `private_key`. [Apple setup](https://docs.cloud.google.com/identity-platform/docs/web/apple).

[Provider bootstrap](../scripts/identity_platform.py) reads a pinned secret version;
default execution only inspects configuration. `--apply` sends credentials directly to
Identity Platform and checks public configuration readback. Keep secret JSON out of
shell arguments, images, Git and logs; add versions through approved Secret Manager tooling.

```sh
python scripts/identity_platform.py --project iz27-platform-dev --provider google --secret-version 1
python scripts/identity_platform.py --project iz27-platform-dev --provider apple --secret-version 1
# After provider-change authorization, repeat each command with --apply.
```

## Release and verification

1. Back up the database; apply migration `0204` through the existing release migration process. Do not migrate the retained database as a test.
2. Layer [consumer identity values](../deploy/helm/events-concierge/values-consumer-identity.example.yaml) over the reviewed non-mock release. Set project, restricted key, exact auth domain, both providers, approved legal versions/URLs and the stable owner IAP subject. Disable the legacy OIDC BFF. Runtime mounts exclude its client secret.
3. Run [production validation/canary](../docs/production-operations.md#first-release-acceptance), then explicitly enable signup for a bounded pilot. Keep general access restricted until acceptance succeeds.
4. In Google Chrome and Safari, verify Google and Apple signup/cancellation, unchecked consent, reload/logout, expiry, disabled/revoked users, changed terms, saved filters/preferences, two-account isolation and same-account erasure. Verify guests can browse and cannot save; another verified Google email must receive admin `403`.
5. Exercise missing/wrong Origin, CSRF, project, provider, cookie/state replay, concurrent sign-in and erasure/provider outages. Never put test tokens or provider errors into evidence.

- [Unit checks](../tests/unit/test_consumer_identity.py), [PostgreSQL checks](../tests/integration/test_consumer_identity.py) and [browser fixtures](../tests/e2e/test_next_consumer_identity.py) establish implementation behavior. Fixtures do not prove real Google/Apple configuration or deployed acceptance.
- Keep sign-in responses uncached. The sign-in document sends only the origin on cross-site requests so [browser-key restrictions](https://docs.cloud.google.com/docs/authentication/api-keys#websites) work; auth API errors send no referrer. Validate edge security headers and SDK/iframe connectivity on the served origin.
- Rollback: disable new signup, retain Identity Platform/users and migration `0204`, and restore a tested compatible image/configuration. Reverting to legacy OIDC does not migrate account identities; managed sessions must fail closed under a different authority. Never destroy the identity state or weaken tenant isolation to restore access.
- Monitor signup failures, identity-service availability, erasure failures and Identity Platform quotas/costs through the existing operations/billing workflow.
