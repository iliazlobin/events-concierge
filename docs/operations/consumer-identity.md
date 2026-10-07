# Consumer accounts

Google signup uses GCP Identity Platform; Apple is available for later activation.
Browse the published catalog without an account; sign in to store personal data.
Legal acceptance follows the configured release mode.
Implementation is available; provider setup and deployed acceptance remain release gates.

## Access

| Caller | Allowed |
| --- | --- |
| Guest | Events, Map, Calendar, Entities/Graph, event details and provider links. |
| Signed-in user | Own profile, preferences, saved filters and account erasure. PostgreSQL RLS and CSRF protect writes. |
| Admin | `/admin` routes; Google IAP admits the configured member; the operator API checks the verified identity against [configured RBAC](operator-access.md). Consumer login grants no admin role. |

- Default `EC_CONSUMER_LEGAL_MODE=required`: publish approved, versioned HTTPS Terms of Service and Privacy Policy pages. The checkbox starts unchecked; the API records account binding and acceptance atomically.
- The owner may explicitly select `EC_CONSUMER_LEGAL_MODE=deferred` for a release and unset all four `EC_SIGNUP_TERMS_*`/`EC_SIGNUP_PRIVACY_*` fields. Signup/login/logout remain available with managed identity, sessions, CSRF, tenant isolation and erasure checks. No legal checkbox, document links or acceptance receipts are created; this setting is deployment-owned, never caller-selected.
- To restore `required`, publish real versioned HTTPS documents and configure their versions/URLs. Existing accounts and sessions must accept them before personal data access resumes; logout still works. Preserve account IDs and genuine receipts; never backfill acceptance for deferred accounts.
- Accounts bind the verified Identity Platform project/UID, never email. Different provider identities stay separate; account linking is not implemented. Apple private-relay addresses are supported.
- ID tokens exist briefly in SDK memory. The [Admin SDK](https://firebase.google.com/docs/auth/admin/verify-id-tokens) checks the Firebase project/UID, provider, freshness and revocation. Google login also requires its signed OAuth ID token, checked against [Google’s verification contract](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token): Google signature, issuer, exact configured client audience, authorized party when present, recent issuance and verified email. Its `sub` must equal the sole Google ID in [Firebase's signed provider identities](https://firebase.google.com/docs/rules/rules-and-auth).
- Identity Platform keeps multiple accounts per email, so [social accounts may have no primary email](https://firebase.google.com/docs/auth/admin/manage-users#retrieve_user_data). The signed Google email supplies contact metadata; it never selects or links an account. A conflicting primary email rejects sign-in. Apple retains the verified Firebase email requirement and rejects an extra Google token. Sessions, reauthentication and erasure remain bound to the Firebase project/UID; an existing account's stored contact email is not automatically rewritten.
- The browser receives an opaque Secure/HttpOnly session and CSRF cookie. Default session: eight hours; managed disable/revocation checks cache for at most 60 seconds, with no stale fallback.
- Erasure requires a fresh, same-account provider login. The erasure worker fences sessions, removes the managed identity and completes application cleanup; failed identity deletion keeps erasure incomplete.
- Changing legal content requires a new version and immutable URL. Existing accounts must accept it again. Acceptance receipts are tenant-scoped and removed during account erasure.

## GCP setup

[Terraform root](../../infra/terraform/environments/consumer-identity) owns Identity Platform,
the restricted browser key, provider secret containers and narrow per-process IAM.
It adds no GKE nodes or database. Use existing application-project service accounts and
the protected application state bucket; never adopt shared foundation/network/cluster state.

| Resource | Configuration |
| --- | --- |
| Identity Platform | Google/Apple only; anonymous/password/phone signup and automatic email linking disabled. `signup_enabled` defaults to `false`; enable only for an approved pilot/release. |
| Browser API key | Exact application/auth domains; Identity Toolkit and Secure Token APIs only. Public identifier, not user authority. |
| API Workload Identity | `firebaseauth.users.get`; no provider secrets or identity deletion. |
| Erasure Workload Identity | `firebaseauth.users.get` and `firebaseauth.users.delete`. |
| Provider secrets | `ec-consumer-google`, `ec-consumer-apple`; numbered Secret Manager versions, operator access only. Values never enter Terraform state or containers. |
| Admin access | Google IAP on the `/admin` backend Service; shared Next.js, separate operator API checking the private RBAC policy. |

1. Use `https://events.iliazlobin.com` as the consumer origin in `iz27-platform-dev`. [Public routing](public-access.md) admits guests and protects `/admin` with Google IAP. Verify the active CLI account independently of the [Identity console](https://console.cloud.google.com/customer-identity/providers?project=iz27-platform-dev&authuser=4).
2. Prepare the [backend example](../../infra/terraform/environments/consumer-identity/backend.tf.example) and [variables](../../infra/terraform/environments/consumer-identity/identity.tfvars.example). Initialize this new app-owned state, review a saved plan, then apply only after deployment authorization.
3. Configure provider credentials below and the approved legal mode. Record the approved `signup_enabled` setting; new setups default to `false`. Enabling a bounded pilot permits the first real signup test, not a release-acceptance claim.

For the existing `iz27-platform-dev` project, `foundation_owned_services` excludes its foundation-owned IAM and Secret Manager APIs from this state. The identity APIs, restricted browser key, provider containers and narrow IAM remain application-owned; never import shared resources or another product's OAuth clients/branding.
The provider and bootstrap requests use the selected identity project for quota; the caller needs `serviceusage.services.use` there.

- Set `EC_IDENTITY_PLATFORM_GOOGLE_CLIENT_ID` to the **enabled consumer Google provider's web OAuth client ID**, not the admin IAP client. It is a public identifier; enabled Google login cannot start without it.
- Google sign-in and reauthentication post `id_token` (Firebase) and `google_id_token` (Google), obtained through [the SDK credential](https://firebase.google.com/docs/reference/js/auth.googleauthprovider#googleauthprovidercredentialfromresult). Missing, invalid or mixed-account proof fails closed. Deploy the matching API, frontend and client-ID configuration together; no schema or email-linking policy change is required.
- The existing Google JWKS resolver uses the fixed Google HTTPS endpoint and RS256, with a five-second fetch timeout, five-minute cache and 30-second refresh cooldown. No new secret is needed. Native datastore/Temporal checks do not verify this provider flow; real signup, repeat login, reauthentication and logout remain browser acceptance gates.

Configure `EC_IDENTITY_PLATFORM_PROVIDERS='["google.com"]'` for the initial release.
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
`events.iliazlobin.com` is the application domain; the SDK auth domain stays
`<project-id>.firebaseapp.com` unless a separate custom auth-domain setup is completed.

**Apple:** requires Apple Developer membership, a Sign in with Apple-enabled app,
Services ID, Team ID, Key ID and private key. Register the same auth-domain callback
and review private-relay email requirements. Secret JSON fields: `client_id` (Services ID),
`team_id`, `key_id`, `private_key`. [Apple setup](https://docs.cloud.google.com/identity-platform/docs/web/apple).

[Provider bootstrap](../../scripts/identity_platform.py) reads a pinned secret version;
default execution only inspects configuration. `--apply` sends credentials directly to
Identity Platform and checks public configuration readback. Keep secret JSON out of
shell arguments, images, Git and logs; add versions through approved Secret Manager tooling.

```sh
python scripts/identity_platform.py --project iz27-platform-dev --provider google --secret-version 1
python scripts/identity_platform.py --project iz27-platform-dev --provider apple --secret-version 1
# After provider-change authorization, repeat each command with --apply.
```

## Release and verification

1. Back up the database; apply migrations through `0208` through the existing release process. `0207` retains real consent receipts; `0208` separately binds accounts for deferred releases with the same identity/erasure fence and no consent writes. Do not migrate the retained database as a test.
2. Layer [consumer identity values](../../deploy/helm/events-concierge/values-consumer-identity.example.yaml) over the reviewed non-mock release. Set project, restricted key, exact auth domain, enabled providers and the approved legal mode; `deferred` requires empty legal versions/URLs. Disable the legacy OIDC BFF. Runtime mounts exclude its client secret. Configure operator identity separately.
3. Run [production validation](release.md#first-release-acceptance) and private transport/readiness checks. With signup explicitly approved, follow [controlled hostname publication](public-access.md#activate) for the deployed canary and real browser acceptance. Failed or unavailable acceptance requires disabling the dedicated public routes; preserve identities/data.
4. In Google Chrome and Safari, verify Google signup/cancellation, reload/logout, expiry, disabled/revoked users, saved filters/preferences, two-account isolation and same-account erasure. In `required` mode verify unchecked consent and changed terms; in `deferred` mode verify no legal links/receipts and test later required-mode activation. Repeat for Apple only when enabled. Verify guests can browse and cannot save; an explicitly unassigned verified identity must receive admin `403`.
5. Exercise missing/wrong Origin, CSRF, project, provider, cookie/state replay, concurrent sign-in and erasure/provider outages. Never put test tokens or provider errors into evidence.

- [Unit checks](../../tests/unit/test_consumer_identity.py), [PostgreSQL checks](../../tests/integration/test_consumer_identity.py) and [browser fixtures](../../tests/e2e/test_next_consumer_identity.py) establish implementation behavior. Fixtures do not prove real Google/Apple configuration or deployed acceptance.
- Keep sign-in responses uncached. The sign-in document sends only the origin on cross-site requests so [browser-key restrictions](https://docs.cloud.google.com/docs/authentication/api-keys#websites) work; auth API errors send no referrer. Validate edge security headers and SDK/iframe connectivity on the served origin.
- Rollback: disable new signup, retain Identity Platform/users and real receipts, and restore a tested compatible image/configuration. Keep `0208` for deferred mode; its downgrade removes only the bootstrap function. Reverting to legacy OIDC does not migrate account identities; managed sessions must fail closed under a different authority. Never destroy identity state or weaken tenant isolation.
- Monitor signup failures, identity-service availability, erasure failures and Identity Platform quotas/costs through the existing operations/billing workflow.

## Built-in OIDC BFF activation

Compatibility path for provisioned accounts; self-service signup uses [managed consumer accounts](#access).

- Set `EC_OIDC_BFF_ENABLED=true`, `EC_MOCK_CLOUD=false`, `EC_UI_AUTH_START_URL=/auth/login`; secret-managed confidential client uses `client_secret_basic`.
- Canonical HTTPS origin; exact `<origin>/auth/callback`; HTTPS issuer/authorization/token/JWKS and asymmetric algorithm allowlist.
- Private profile: `EC_PUBLIC_ORIGIN_PROFILE=private_loopback_https`, exact `EC_PUBLIC_BASE_URL=https://localhost:14443`; callback `https://localhost:14443/auth/callback`. No alternate host/port/path/query; default `remote_https` rejects loopback.
- Trusted local TLS and IAP remain required. Origin configuration does not encrypt dependencies or authorize activation; follow [transport preparation](transport.md#authenticated-discovery-and-encrypted-dependencies).

| Google setting | Required value |
| --- | --- |
| `EC_OIDC_PROVIDER` / tenant claim | `google`; leave `EC_OIDC_TENANT_CLAIM` unset |
| `EC_OIDC_ISSUER` | `https://accounts.google.com` |
| `EC_OIDC_AUTHORIZATION_URL` | `https://accounts.google.com/o/oauth2/v2/auth` |
| `EC_OIDC_TOKEN_URL` | `https://oauth2.googleapis.com/token` |
| `EC_OIDC_JWKS_URL` | `https://www.googleapis.com/oauth2/v3/certs` |
| `EC_OIDC_ALGORITHMS` / scopes | `RS256` / `openid email` |

- Bind verified case-sensitive `sub`, never email: `google_subject_binding(verified_sub)` → `oidc:v1:https://accounts.google.com:<sub>` in `tenants.oidc_subject`.
- Provision through parameter-bound `fn_provision_tenant(uuid, text, text, text)` with approved UUID/contact/relay. Migration `0195` supplies resolver/grant. No automatic signup, rebinding or transfer; unknown/erased subjects fail closed; re-enrollment needs approval.
- **Google limit:** reauthentication/account deletion return `503`; UI exposes `reauth_url=null`. Consent, account selection, callback time and `iat` are not recent-auth proof. Private pilot needs explicit owner acceptance of unavailable self-service deletion.
- Pilot canary: `python -m events_concierge.operations canary --profile private_google_pilot --base-url https://localhost:14443`, plus `--expected-release-revision` and `--expected-image-digest`; trusted CA through `SSL_CERT_FILE`. No HTTP/local-demo/degraded-Temporal overrides.
- Readiness/canary does not prove real callback registration, login/cancellation/logout, expiry, replay or replica-independent sessions; verify those against the IdP.

| Session boundary | Required behavior |
| --- | --- |
| Login | One-shot Redis state/nonce/S256 PKCE; same-origin return; transaction TTL default 10 minutes, max 15 |
| Session | `__Host-ec_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain; TTL 5 minutes–24 hours, default 8 hours |
| CSRF | `__Host-ec_csrf`: Secure, SameSite=Strict, Path=/, no Domain; exact Origin + cookie/`X-EC-CSRF` match + session digest |
| Logout/revocation | Delete Redis session before cookies; outage `503`, no false revocation; ≤32 live sessions/tenant and permanent issuance fence; not IdP logout |
| Custom-claim identity | Explicit tenant UUID claim + pre-provisioned subject; no callback-created accounts |
| Custom-claim erasure | Same-session `POST /auth/reauth`, `prompt=login&max_age=0`, provider `auth_time` ≤2 minutes + bounded skew; exact `DELETE MY ACCOUNT` |
| Recent authentication | `EC_ACCOUNT_ERASURE_RECENT_AUTH_SECONDS` 60–900, default 300; no session extension |
| Accepted erasure | Clear cookies; underway receipt only; no authenticated completion polling |

- Require `identity=ready`; Redis authenticated/TLS, isolated, no eviction, bounded capacity. Callback errors expose only `cancelled`, `not_authorized`, `unavailable`; no tokens/provider detail.
- Keep erasure lock until admitted effects settle, including overdue/cancelled SDK work. Adapter deadline ≤ effect deadline; alert on overrun. Cancellation alone cannot prove termination.
- `EC_TENANT_EFFECT_LOCK_TIMEOUT_SECONDS`: 0.1–30, default 5; `EC_TENANT_EFFECT_TIMEOUT_SECONDS`: 0.1–60, default 30.
- Default canary requires deployment-session identity, login/reauth/logout routes, matching CSRF and unauthenticated reauth `401`; Google pilot is the explicit exception. Local onboarding/tenant-header authentication stays absent.
- Source: [OIDC/session implementation](../../src/events_concierge/adapters/oidc/session.py), [erasure design](../../design/system-design.md#account-erasure), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [Google reauthentication limit](https://developers.google.com/identity/siwg/security-bundle#authentication_time).
