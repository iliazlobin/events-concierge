# ADR-012: Same-origin static consumer shell packaged with the API image

**Status:** accepted — consumer and built-in identity boundary implemented; production field evidence gated · **Date:** 2026-07-21 · **P48 update:** 2026-07-22

## Context

P46 added a responsive consumer surface at `/` and `/app` for onboarding, catalog previews,
durable requests, lifecycle-backed Plans, actionable handoffs, preferences, feedback, and
withdrawal. P47 added an explicit request-to-selected-lifecycle projection, bounded automatic
refresh, a fail-closed CSRF verification seam, and a committed Playwright/CI product gate. P48
added the repository-owned OIDC authorization-code BFF, purpose-bound recent authentication, and
the account-erasure Danger Zone/accepted state. The
product needs a clear ownership boundary for that browser surface: how its files ship, which state
it may hold, how it reaches authenticated product truth, and which production controls remain
deployment responsibilities.

The browser is not a new workflow authority. PostgreSQL remains the queryable source of tenant and
lifecycle truth under RLS, Temporal remains the durable executor, and
[ADR-009](adr-009-email-launch-notification-channel.md) remains the authority for launch
notifications. The UI projects those systems through same-origin API contracts; it does not query
Temporal, call event-source adapters, write calendars, hold provider credentials, or replace email
delivery.

## Options considered

1. **A separately built and deployed SPA.** A React/Vue-style client, Node toolchain, fingerprinted
   bundles, and independent CDN release would provide a broad component ecosystem and independent
   scaling. It would also introduce a second dependency graph, deployable, cache-invalidation
   contract, cross-origin/auth configuration surface, and client/API compatibility window before
   the current MVP needs them.

2. **Server-rendered templates for every product view.** This would keep one runtime and minimize
   browser state, but every navigation, filter, feedback action, and lifecycle refresh would need a
   page or fragment round trip. It would couple presentation templates to HTTP handlers and make the
   current responsive interaction and focus-management behavior harder to keep coherent.

3. **A framework-free static shell served by the same FastAPI process (chosen).** HTML supplies the
   semantic shell, CSS owns desktop/mobile presentation, and one JavaScript module owns view state,
   API calls, and safe DOM construction. Client and API roll as one immutable image, use one origin,
   and require no frontend build service.

A service worker/offline cache is not part of the chosen option. The web manifest is install
metadata only; durable product state remains server-owned and must not be shadowed by an offline
browser cache.

## Decision

### Packaging and serving

The consumer files live under `src/events_concierge/api/static/`:

- `index.html` — semantic welcome/product shell and the four product views;
- `app.css` — visual system, responsive layout, accessibility media queries, and the standalone
  handoff-completion-page styles;
- `app.js` — boot, hash navigation, in-memory state, same-origin API client, rendering, and focus
  management;
- `mark.svg` and `manifest.webmanifest` — product mark and install metadata.

Hatch includes the static directory as package data in the installed Python wheel. The Docker
builder installs that wheel non-editably, and the runtime image copies the installed environment
rather than a mutable source tree. FastAPI serves `/` and `/app` from `index.html`; it serves only
the explicit `app.css`, `app.js`, and `mark.svg` asset allowlist, plus dedicated manifest and favicon
routes. Arbitrary files under the package directory are not web-addressable.

There is no Node dependency, bundler, minifier, frontend development server, or independent client
deployment. A static-source change therefore requires rebuilding and recreating the API image. The
benefit is an atomic client/API rollout; the cost is that frontend and API release cadence cannot be
separated without superseding this ADR.

### Navigation and state ownership

The client is a small hash-routed application. `#/concierge`, `#/plans`, `#/tasks`, and
`#/settings` select already-present semantic sections; they are not server routes. CSS alone changes
the desktop rail into the mobile header/navigation at the 900 px breakpoint, so JavaScript does not
branch on viewport size.

| Owner | State |
|---|---|
| Browser, ephemeral | active hash view, current brief, visible pick batch/cursor, busy state, dialog target, typed erasure phrase, toast timer, and one refresh timer/promise |
| Browser, local demo only | one opaque tenant reference in `localStorage`, sent through the mock-only `X-EC-Tenant-ID` seam |
| Browser, retry identity only | one random erasure request UUID in `localStorage` until acceptance; it is neither authentication nor a status capability and the typed phrase is never stored |
| PostgreSQL under tenant RLS | identity, preference revision, requests, immutable selected-outcome links, feedback-derived profile state, lifecycle-backed Plans, and actionable handoffs |
| Temporal | workflow execution, retries, timers, and signals; never queried directly by the browser |

Boot first reads `/v1/ui-config`, then resolves `/v1/me`. Local mock mode may expose `/v1/onboard`
and retain the opaque tenant reference; production mode has no onboarding route and uses the
repository-owned same-origin `/auth/login` BFF entry point. Preview uses `POST /v1/feed` and creates no durable
request. “Find and handle it” uses `POST /v1/requests`, whose accepted state means the database-backed
brief is saved—not that registration has completed. Plans and To do render tenant-scoped PostgreSQL
projections, and authenticated task completion requests verification before any calendar claim.
When the parent workflow selects a committed child result, it appends one RLS-scoped
`request_outcome_links` identity row. Exact replay converges, rebinding to a different lifecycle is
rejected, and the Recent briefs projection reads the linked lifecycle's current state. It never
infers a selected outcome from timestamps or candidate order, and it exposes only allowlisted
selected-or-later states rather than a pre-selection or internal failed attempt.

Settings contains a separate Danger Zone. The erasure dialog explains irreversible scope and keeps
its submit control disabled until the exact phrase `DELETE MY ACCOUNT` is present; the server
validates the same literal. Production additionally requires a purpose-bound recent-auth grant.
After the durable fence is accepted, the API clears browser cookies and the shell discards its local
references and renders a non-polling “underway” receipt. It deliberately does not call an accepted
request “complete,” because tenant-wide session revocation prevents authenticated progress polling
and provider/backup deletion has separate operational evidence.

The signed-in shell runs automatic work only while a request created within the prior six hours is
still `received`/`started` without a selected outcome. While visible, one timer chain rechecks Recent
briefs at a jittered 30-second base cadence and backs off exponentially toward five minutes after
failures. A changed request fingerprint refreshes Plans and To do once; per-resource generations
prevent a slower poll from overwriting a newer navigation/manual result. Hidden, signed-out, aged-out,
and resolved states stop the timer, while visibility return triggers an immediate eligible check. A
transient background failure preserves the last rendered truth. This is pending-request convergence,
not a permanent all-collection poll, second notification channel, or browser-owned state machine.

### Security and authentication boundary

The shell and JSON API share one origin. Fetches use same-origin credentials and no browser-held
provider token. Production tenant identity is resolved by the repository-owned OIDC BFF through one
coherent `BrowserSessionLifecyclePort`; the local tenant header and browser storage are development
scaffolding and must never be enabled for public traffic. The supported production profile fixes
`EC_UI_AUTH_START_URL=/auth/login`; an alternate external BFF is unsupported until it has an
explicitly parameterized UI, tenant-wide revocation, recent-auth, and canary contract.

The shell is served without long-lived caching and with explicit `Content-Security-Policy`,
`Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, and `Permissions-Policy` headers:
scripts, styles, connections, and fonts are same-origin; images are same-origin or data URLs;
objects and foreign framing are denied; forms stay same-origin; MIME sniffing is disabled; camera,
microphone, geolocation, and payment capabilities are disabled. Dynamic event destinations accept
HTTPS or same-origin local HTTP only, reject embedded credentials, open in a new tab, and use
`noopener noreferrer`. Rendering uses DOM text nodes rather than unsafe HTML sinks.

The signed-email handoff page remains a distinct, deliberately inert surface with a stricter
no-referrer policy and deliberate POST. Its bearer capability is never exposed through the
authenticated consumer projection. The browser task list uses the tenant-bound
`/v1/me/tasks/{task_id}/done` contract instead.

Every authenticated consumer mutation depends on `CsrfProtectionPort` after authentication and
account provisioning; verifier rejection returns `403`. The local no-op exists only alongside
explicit mock tenant-header authority, where there is no ambient cookie. The built-in path uses
one-shot state/nonce/S256-PKCE login transactions; validates configured issuer, audience, `azp`,
asymmetric algorithm, tenant, subject, and JWKS; holds only fixed-TTL opaque session handles in
Secure `__Host-` cookies; and keeps CSRF digests, a capped pseudonymous tenant session index, and
revocation authority in Redis. Mutations require an exact configured `Origin` and the CSRF cookie
echoed in the configured header. Logout revokes server authority before clearing cookies, and
readiness fails when Redis cannot safely resolve or revoke sessions.

`POST /auth/reauth` is itself current-session and CSRF protected. Its one-shot transaction binds
purpose=`account_erasure`, session digest, tenant, subject, state, nonce, and PKCE; the provider
request includes `prompt=login&max_age=0`. Callback requires numeric recent `auth_time`, exact same
identity, and the still-live bound session before atomically granting a short server-timestamped
recent-auth window. Client timestamps and flags are never authority. Real client registration,
secret-manager provisioning, HTTPS/HSTS, Redis isolation/capacity, subject provisioning, and a
production browser canary remain launch evidence; the repository implementation does not prove
those deployment facts by implication.

### Notification authority

ADR-009 is unchanged: email to the user's real address is the launch notification channel, backed
by the notification outbox/ledger. The browser's pending-request convergence loop, To-do badge, and
handoff list are product freshness mechanisms, not a replacement delivery mechanism or evidence
that a user was notified. Adding push or browser notifications later must preserve ADR-009 delivery
semantics until a separate ADR explicitly changes the launch notification authority.

## Consequences

**Easier:** one dependency graph, origin, image, CSP, and rollback unit; no CORS or independent
client-version negotiation; production tokens stay at the BFF boundary; the browser reads narrow
consumer projections instead of reconstructing workflow truth; local operation has no frontend
installation step.

**Harder / risks accepted:** every UI edit rebuilds the API image; assets are intentionally
unfingerprinted and `no-cache`, so this shape does not provide CDN-grade immutable caching; one
JavaScript module and one stylesheet require discipline as the surface grows; there is no offline
mode; only recent request selection receives automatic convergence, while later lifecycle changes
require navigation/manual refresh rather than push delivery. A separate frontend becomes appropriate
if independent deployment, a substantial component system, or client-side performance requirements
outweigh the one-image simplicity.

**Production activation gates:** real confidential OIDC client and secret, provisioned subject/tenant
mapping, public HTTPS/HSTS edge controls and reverse-proxy header preservation,
isolated/capacity-tested Redis, real runtime
providers and credentials, and an authenticated browser canary covering login, purpose-bound
reauthentication, CSRF rejection/acceptance, logout/expiry/revocation, preview versus durable truth,
Plans, To do, account-erasure acceptance, safe handoffs, and degraded Temporal status. Asset/header
checks must run against the exact immutable image. The committed Playwright/CI suite is the
repository product gate, but deterministic fixtures are not production identity evidence or
complete WCAG conformance.

This ADR records the implemented local architecture and does not claim that the broader production
launch gates in the [operations runbook](../docs/operations/README.md) are closed.
