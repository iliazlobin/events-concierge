# Account & Settings Vertical — Design Proposal

Historical proposal (26 August 2026). Current discovery scope: [PROJECT.md](../PROJECT.md#current-milestone-private-discovery-candidate); identity/erasure gates: [production operations](../docs/operations/consumer-identity.md#built-in-oidc-bff-activation); UI acceptance: [manual checks](../docs/manual-test-plan.md). The build order below is not the current task queue.

**Status:** DRAFT for owner ruling · **Date:** 2026-08-26 · **Targets:** `web/` (Next.js consumer tree)
**Verified against:** working tree at `f3c5875` + uncommitted work. Migration head `0154`; `web/components/concierge-app.tsx` is 1202 lines (profile dot at `:1052-1065`). The tree is being edited concurrently — **re-derive line numbers and the Alembic head at implementation time; cite by symbol, not by line.**

---

## 1. What we're building

This is the account surface for a system that spends the user's authority while they are not watching:
it discovers events, decides which to act on, registers or RSVPs on third-party sites under the user's
name, and writes the result to their calendar. Three fields on this surface are therefore
authority-bearing rather than cosmetic — `notify_email` is the delivery address for bearer capability
links (`POST /v1/tasks/{token}/done` is deliberately outside the CSRF/session boundary), the
browser-session list is the revocation surface for that authority, and any future autonomy or spend
control is a consent record, not a preference. Display name, photo, time zone and interests are
presentation. The design keeps the two tiers in different tables, behind different endpoints, with
different auth dependencies, so one generic `PATCH /v1/me` can never collapse the verification rule
into a field-level `if`.

Two facts reframe the work before any of it is designed.

**The backend is far ahead of the Next.js UI.** `PUT /v1/preferences`, `GET /v1/requests`,
`GET /v1/registrations`, `GET /v1/tasks`, `POST /v1/unrsvp`, `POST /v1/me/erasure-requests`,
`POST /auth/logout` and — most importantly — `POST /v1/requests` all have **zero callers anywhere in
`web/`**. The Next tree is browse-only: it cannot start a durable ask, cannot show what the concierge
did, and cannot undo it. Much of the value here is wiring finished, correct backend to a UI, not
building backend.

**These capabilities are not unbuilt — they are unported.** The legacy static shell
(`src/events_concierge/api/static/`) ships a full Settings page, a Plans page, a To-do page and a
working sign-out. The Next.js app is a *regression* against it on the account and lifecycle half of
the product while being far ahead on catalog and entities. This vertical is a **port plus extension**,
and it needs a parity matrix and a cutover plan, not a greenfield design.

---

## 2. Blockers that outrank the feature

These are live defects found while mapping. Items 1–3 must land before or with any writable surface.

**B-1 · Deployment mode has no account-creation path.** `tenant_repo.add` is reachable from exactly two
places: `slice_demo.py` and the `/v1/onboard` handler, which sits inside `if settings.mock_cloud:` and
whose own docstring says *"production has no unauthenticated bootstrap route."* `/auth/callback`
requires `tenant_repo.get(...)` to already return a row with a matching `oidc_subject`, else 401.
`fn_provision_tenant` is wired at `adapters/postgres/tenant_repos.py:226` and never called on the
authenticated path. **A first-time deployment user cannot sign in at all.** This outranks the whole
vertical: a profile page for accounts that cannot exist is not shippable, and it means `notify_email`
— the field §5.3 builds its argument around — is currently *unsettable* in deployment, since the OIDC
scope is literally `"openid"` with no userinfo call.

**B-2 · `web/lib/api.ts` sets no CSRF header.** It sets `Accept`, optional `X-EC-Tenant-ID` and
optional `Content-Type` — nothing else. Every consumer mutation depends on `CsrfProtectedTenant`, so
`refreshCatalogEntity` and `recordFeedback` (the implicit-affinity loop) are already latent 403s in
deployment. The legacy shell implements this correctly at `api/static/app.js:115-137` — port it.
Because `EC_MOCK_CLOUD=true` makes the CSRF adapter an explicit no-op, a local-first build passes
locally and 403s in deployment with no local reproduction. The test must assert the header is *set*,
not that the call succeeds.

**B-3 · Every successful OIDC login lands on a 404.** `GET /auth/login` defaults
`return_to="/app"`, the callback 303s there, and `_safe_return_path` permits only `/` or `/app` — but
`web/app/` has no `app/` route and `next.config.ts` has no rewrites. `ReauthenticationBody.return_to`
defaults to `/app#/settings`, so the step-up hop is broken the same way.

**B-4 · `web/tests/account-*.test.mjs` would be run by nothing.** The npm scripts glob `admin-*`,
`event-*`, `filter-suggestions`, `chat-intent`, `runtime-api-proxy`, and CI enumerates those five by
name. Add `test:account` to `web/package.json` **and** to `.github/workflows/ci.yml` in the same PR.

**B-5 · The Next app ships with no CSP.** No `middleware.ts`, no `headers()` in `next.config.ts`. The
FastAPI shell's `_UI_SECURITY_HEADERS` do not reach it. The vertical that first puts PII forms on a
Next page owns adding `web/middleware.ts`.

---

## 3. The dropdown menu

Replaces the two-branch profile dot in `.site-header`.

```
┌───────────────────────────────────────────────────────────────────────────┐
│ ◈ Events Concierge   Chat Events Map Calendar Entities          ( A ) ⌄   │  sticky, z-40
└──────────────────────────────────────────────────────────┬────────────────┘
                            ┌──────────────────────────────┴──────────────┐
                            │  Ada Lovelace                               │  identity block
                            │  ada@example.com                            │
                            ├─────────────────────────────────────────────┤
                            │  ◐  Profile            Name, photo, zone    │  → /settings
                            │  ✦  Interests          What it looks for    │  → /settings/taste
                            │  ☑  Activity           Asks, registrations  │  → /settings/activity
                            │  ⚠  Account and data   Sign-in, deletion    │  → /settings/account
                            ├─────────────────────────────────────────────┤  local_demo ONLY
                            │  🔧 Open ingestion administration           │  <a href="/admin">
                            ├─────────────────────────────────────────────┤
                            │  ⇥  Sign out                                │  --danger
                            └─────────────────────────────────────────────┘
                             min 268px / max min(300px, 100vw − 24px)
                             max-height: min(70vh, 100vh − var(--header-height) − 96px)
```

Six items max, per `design/product-opinion.md:40` ("a bounded brief/control surface … must not become
an infinite catalog/feed").

**Mechanics.** New `web/components/account-menu.tsx`. The five existing popovers are value pickers
(`listbox`/`option`) or editors (`dialog`); this is a *command* menu, so `menu`/`menuitem` with roving
`tabIndex` are new — but every dismissal mechanic is lifted verbatim from `compact-combobox.tsx`:
wrapper `onBlur` → `setTimeout(…, 0)` → `!control.contains(document.activeElement)` → close; items
`preventDefault()` on `onMouseDown`; Escape closes and restores focus via `requestAnimationFrame`.
**No document listener, no portal, no focus trap** — a menu button must not trap focus (Tab closes it
and lets focus move on, per the ARIA APG). The trigger calls `event.currentTarget.focus()` first,
because Safari does not focus a button on click and the blur contract needs the wrapper focused.

Navigation items are real `<a href>` so ⌘-click and middle-click open a tab — today's `/admin`
affordance behaves that way and must keep behaving that way, keeping its exact `href` and its
accessible name.

**Stacking.** `.site-header` is `position: sticky; z-index: 40` with a backdrop filter, and
`.app-shell` is `isolation: isolate`. Both create stacking contexts, so the panel's own z-index only
orders it against header siblings. It clears `.filter-shell` (35) and `.app-main` (1) because the
*header's* context outranks them — and it can **never** paint above `.mobile-nav` (`z-index: 60`) at
any z-index. The `max-height` clamp is a hard requirement with a static test, not a nicety.

**Sign-out has two branches.** `deployment_session` → `POST config.logout_url` **with the CSRF
header**, then `clearSession()` + `clearCatalogCache()` + `location.assign("/")`. On 503 the server
deliberately preserves the cookie, so the UI must show "Couldn't sign out — try again" and must
**not** clear local state. In `local_demo` the label is "Leave this session" with no server call. Both
branches must call `clearCatalogCache()` — it is tenant-keyed and its own docstring says it "runs on
sign-out and account erasure," which no code currently does. Failure must route to `ToastRegion`, not
`setOnboardingError` (that state only renders when `sessionState === "onboarding"`, so the 503 branch
would produce a silent stuck spinner).

**Not in the menu, with reasons:** no theme toggle (dark is hard-coded), no language (no i18n), no
plan/billing badge (no billing table, no price, no payment instrument in 154 migrations;
`Permissions-Policy` disables `payment=()`), no "switch account" (`oidc_subject` is a UNIQUE
insert-once binding).

---

## 4. Page map

| Route | Purpose | Slice |
|---|---|---|
| `/settings` | **Profile** — display name, time zone, read-only email + relay inbox, avatar | **v1** |
| `/settings/taste` | **Interests** — 8 chips + bounded free entry; writes `PUT /v1/preferences` | **v1** |
| `/settings/account` | **Account and data** — sign-in facts, read-only "How it works" cards, Danger Zone | **v1** |
| `/app` | Post-login and post-reauth landing; resolves the `#/settings/…` fragment | **v1 (fixes B-3)** |
| `/settings/activity` | Asks / registrations / to-dos, with withdraw | slice 4 |
| `/settings/security` | Active sessions, sign out everywhere | slice 7 |
| — | Notification controls (quiet hours, digest) — **FR-15.3/AC-89** | slice 6, only with the emitter |
| — | Email change | slice 8, gated (§9) |
| — | Source connect/disconnect — **FR-2.11/2.12** | see open question 5 |
| — | Autonomy dial, spend limits, billing | **not in scope** — §6.5 |
| — | Data export | see open question 6 |

Four routes, not eight. Notifications, autonomy and connections are **cards inside `/settings/account`**,
not routes of their own — three routes that configure nothing would be exactly the surface drift
`product-opinion.md:40` forbids.

```
web/app/settings/layout.tsx           server; <SessionProvider><SettingsShell>
web/app/settings/page.tsx             "Profile"          → <ProfilePanel/>
web/app/settings/taste/page.tsx       "Interests"        → <TastePanel/>
web/app/settings/account/page.tsx     "Account and data" → <AccountPanel/>
web/app/app/page.tsx                  → <AppLanding/>   (fixes B-3)
web/components/account-menu.tsx
web/components/session-provider.tsx
web/components/settings/{settings-shell,profile-panel,taste-panel,account-panel,
                         danger-zone,settings-field,confirm-dialog,toast-region}.tsx
web/lib/{session.ts,return-path.ts,profile-form.ts}
```

No new stylesheet: `/settings` inherits `globals.css` under the root layout.

---

## 5. Edit Profile

### 5.1 Fields

| # | Field | Control | Bounds | Writable |
|---|---|---|---|---|
| 1 | Photo | initials tile (v1) → `AvatarPicker` | server re-encodes to 256×256 WebP ≤32 KiB | see OQ 2 |
| 2 | Display name | `<input maxLength={64} autoComplete="name">` | NFC-normalized, whitespace-collapsed, 1–64 codepoints, no `Cc`/`Cf` except ZWJ/ZWNJ, no bidi overrides U+202A–202E / U+2066–2069, not all-punctuation | **yes** |
| 3 | Notification email | read-only + `<details>` "How do I change this?" | — | **no** (§5.3) |
| 4 | Relay inbox | read-only, monospace, copy button | — | **never** (ADR-011) |
| 5 | Time zone | `CompactCombobox` over `Intl.supportedValuesOf("timeZone")` | IANA; must exist in `pg_timezone_names` | **yes**, see OQ 7 |
| 6 | Account created | read-only date | — | no |

Field 5 reuses `CompactCombobox` verbatim — it already carries the full keyboard and ARIA contract.

**Cut from v1, deliberately:** `locale` (nothing reads a locale; `<html lang="en">` is hard-coded), and
`home_city` / `home_lat` / `home_lon` (no reader; lat/lon is sharp residential PII with no consent
story; `home_city_norm` would copy a catalog index key into the tenant family, the cross-family path
ADR-001 forbids; and `domain/dedup.normalize_city` returns `None` for a name with no ASCII
alphanumerics, so 東京 or Москва would fail a both-or-neither CHECK as a raw 500-class
`check_violation`). Home city lands in the slice that ships its reader.

Bidi-override rejection is not paranoia: the name renders adjacent to product chrome, and
`RIGHT-TO-LEFT OVERRIDE` lets a name visually rewrite the copy around it.

### 5.2 Wireframe

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  ← Back to events            ◈                          ada@example.com        │ sticky, 74px
├───────────────────┬────────────────────────────────────────────────────────────┤
│ ◐ Profile         │  Profile                                                   │
│ ✦ Interests       │  How the concierge addresses you.                          │
│ ☑ Activity        │                                                            │
│ ⚠ Account & data  │  ┌──────────────────────────────────────────────────────┐  │
│                   │  │  Photo                                               │  │
│  .settings-nav    │  │   ┌──────┐                                           │  │
│  232px, sticky    │  │   │  A   │   [ Upload photo ]   [ Remove ]           │  │
│                   │  │   └──────┘   PNG/JPEG/WebP · cropped to 256px        │  │
│                   │  │    84px      ▐███████████░░░░░░░▌ 62%                │  │
│                   │  └──────────────────────────────────────────────────────┘  │
│                   │  ┌──────────────────────────────────────────────────────┐  │
│                   │  │  Display name                                        │  │
│                   │  │  ┌────────────────────────────────────────────────┐  │  │
│                   │  │  │ Ada Lovelace                                   │  │  │ aria-invalid
│                   │  │  └────────────────────────────────────────────────┘  │  │
│                   │  │  Up to 64 characters.                    ← __hint    │  │
│                   │  │  Enter the name the concierge should use. ← __error  │  │ role="alert"
│                   │  ├──────────────────────────────────────────────────────┤  │
│                   │  │  Notification email     ada@example.com      (fixed) │  │ read-only
│                   │  │  ▸ How do I change this?                             │  │ <details>
│                   │  ├──────────────────────────────────────────────────────┤  │
│                   │  │  Relay inbox   ada-7f2c@u.concierge.test   [ Copy ]  │  │ read-only
│                   │  │  Where sign-in codes for you are received.           │  │
│                   │  ├──────────────────────────────────────────────────────┤  │
│                   │  │  Time zone     [ America/Los_Angeles           ⌄ ]   │  │
│                   │  └──────────────────────────────────────────────────────┘  │
│                   │   Unsaved changes.        [ Cancel ]  [ ◐ Save changes ]   │
└───────────────────┴────────────────────────────────────────────────────────────┘
                                                       ┌────────────────────┐
                                                       │ ✓ Profile saved.   │ role="status", 4s
                                                       └────────────────────┘
```

At ≤760px the sidebar becomes a horizontally-scrollable pill strip; the strip scrolls, the page body
never does (NFR-16, 390px).

**Empty / first-run states, required copy:** profile never saved (name field empty, placeholder "How
should the concierge address you?"); no photo (initials tile from the email's first character, with
the fallback chain in §5.5); interests none chosen ("Pick a few starting signals — the concierge
refines from there."); activity with no rows (three distinct empties, not one shared blank).

### 5.3 Why email is read-only in v1

`tenants.notify_email` is resolved **at send time** by `SesNotificationAdapter.send`, and the outbox
emails `HANDOFF_AVAILABLE` bodies containing `/v1/tasks/{completion_token}/done` — a bearer capability
that mutates the registration lifecycle with no session and no CSRF. Anyone who can change
`notify_email` without proving control of the new mailbox receives those capabilities. In this system
specifically, it is an account-takeover primitive.

`ec_app` holds `SELECT` only on `public.tenants`, and `fn_provision_tenant` is insert-once, so the
change needs a new SECURITY DEFINER capability. That is the small part. Four blocking prerequisites:

1. **The step-up grant is purpose-blind and replayable.** `_REAUTH_PURPOSE` is the literal string
   `"account_erasure"`; `verify_recent_auth` checks only tenant match and recency — never a purpose —
   and never clears `recent_auth_at`. Wiring email change to the existing gate means an erasure
   step-up silently authorizes an email change for the rest of the window.
2. **In-flight notifications re-target silently.** Committing re-points every already-projected outbox
   row, including live handoff capabilities. The obvious fix — revoke outstanding completion tokens —
   **does not exist and cannot be cheaply built**: `ec_app` has no DML on `public.handoff_tasks`, and
   `fn_attach_handoff_completion_token` is attach-once with no clear counterpart. The workable fix is
   to stamp the recipient binding on the outbox record at projection time.
3. **The verification link cannot resolve its own tenant.** `tenant_email_changes` would be FORCE-RLS,
   and a link opened from the new mailbox has no `app.tenant_id`. It needs a **digest-keyed** SECURITY
   DEFINER resolver — `fn_resolve_tenant_email_change(p_token_hash text)` — the same shape as
   `fn_resolve_handoff_completion_token`. Passing a tenant uuid into the capability is wrong on both
   the house rule and the mechanics.
4. **Rider B27** accepts the email-only launch channel *only because* it is "mitigated by onboarding
   address verification + bounce alerting." Neither exists.

**And B-1 supersedes all four in deployment:** with no provisioning path, there is no `notify_email` to
edit. Fixing B-1 is what decides whether the address is IdP-derived (and therefore properly read-only)
or user-supplied at onboarding (and therefore needing the full change flow).

### 5.4 Avatar

The owner asked for a photo, so it is costed rather than waved away — and the cheapest correct version
is **not** the object store.

**Rejected: object-store storage.** The only injected `ObjectStorePort` is claim-check rooted, its
`delete_tenant` purges exactly `{root_prefix}/{tenant_id}/`, and the bucket carries lifecycle rules
that Delete at `claim_check_ttl_days` plus `versioning { enabled = true }` with a 7-day noncurrent
Delete. Avatars there either vanish at 30 days, or need a Terraform change on a
`prevent_destroy = true` bucket — and GCS lifecycle conditions are inclusive-only, so the existing
rule must be *narrowed* with `matches_prefix`, not supplemented. It would also need a new
`TenantEffectKind` member (the enum is closed and has no media member), `ObjectStorePort.delete_object`
plus a matching edit to `_DIRECT_PORT_METHODS["object_store"]`, and a deleted photo would still
survive 7 days as a noncurrent generation.

**Chosen: bounded `bytea` in its own tenant-owned table.** One ≤32 KiB row per tenant,
`tenant_id uuid PRIMARY KEY REFERENCES public.tenants(tenant_id) ON DELETE CASCADE`. Erasure is the
cascade; no Terraform change, no new port, no new effect kind, no noncurrent residue. At 100k accounts
with full adoption that is ≤3.2 GB, TOAST-ed out of line and never selected by the hot `/v1/me` read.
Trade-off to accept knowingly (see OQ 8): it puts user image bytes into the primary DB's backup/PITR
footprint.

**Transport: raw-body POST, not multipart.** `POST /v1/me/avatar` with
`Content-Type: image/png|jpeg|webp` and raw bytes. Three wins: `image/*` is not a CORS-simple content
type, so a cross-origin form cannot reach the route; **`python-multipart` never becomes a dependency**;
and there is no `filename` field, which deletes the path-traversal class at the source.

**Fits the existing caps with no change.** The client crops and re-encodes to 256×256 WebP before
upload (`canvas.toBlob` down a `[0.86, 0.74, 0.62]` quality ladder until ≤32 KiB — typically 8–20 KB),
comfortably under `_MAX_REQUEST_BODY_BYTES = 64 * 1024` (global ASGI middleware, no per-route
exemption) and the proxy's `maxRequestBytes`. Neither cap moves and no dedicated Next proxy route is
needed. 256, not 512: a 512² photographic re-encode routinely exceeds 32 KiB, so a 512 ceiling fails
non-deterministically on image entropy.

**The one real cost: Pillow.** The server must not trust the client's re-encode. Magic-byte sniff (the
declared `Content-Type` is read and discarded); check `img.size` **before** `img.load()` (reject any
axis > 4096 or `w*h > 8_000_000` — a 32 KiB PNG can declare 40,000²); set `Image.MAX_IMAGE_PIXELS` at
module import; reject `n_frames > 1`; then `ImageOps.exif_transpose` → `convert("RGBA")` → center-crop
→ LANCZOS to 256² → **paste into a fresh canvas** → `save(..., "WEBP", quality=82, exif=b"",
icc_profile=None)`. The re-encode — not any header check — is what strips EXIF GPS and neutralizes
polyglots, and it is why `width_px`/`height_px` are `NOT NULL`: only a server that actually decoded can
populate them. `image/svg+xml` fails the decode structurally and must **never** be allowlisted — served
same-origin under `img-src 'self'`, a stored SVG is stored XSS against the origin that owns the
readable CSRF cookie.

**Serving:** `GET /v1/me/avatar` — self-only, tenant from the session, **never id-addressable** (an
id-addressable avatar is a tenant-enumeration oracle in a product with no public-profile concept).
`Content-Type` is a server constant from the closed CHECK vocabulary, plus `X-Content-Type-Options:
nosniff`, `Content-Disposition: inline`, `Cross-Origin-Resource-Policy: same-origin`,
`ETag: "<checksum_sha256>"`, `Vary: Cookie`, and **`Cache-Control: private, max-age=0,
must-revalidate`** — *not* `no-store`, which would forbid storing the response and make the promised
304 unreachable while re-downloading the header dot on every document navigation. Same-origin bytes
satisfy the shipped CSP with no edit to `_UI_SECURITY_HEADERS` and no edit to the canary gate. Gravatar
and the OIDC `picture` claim are both out: the scope is literally `"openid"`, there is no userinfo
call, and either would require relaxing both the header set and `operations/canary.py`.

`checksum_sha256` is the concurrency token — a whole-value replace, so content addressing *is* the
optimistic control and a byte-identical re-upload is a no-op.

### 5.5 Save contract

**Pessimistic commit**, with the crop preview as the one optimistic exception. The write carries a
caller-minted revision and 409s on a stale one, so optimistic mutation means rolling back *and*
refetching *and* explaining; the saved name feeds the header dot, and a name that appears then reverts
is worse than a 400ms spinner. House precedent is explicitly pessimistic
(`admin/source-configuration-editor.tsx` turns 409 into "This source changed while you were editing.").

Dirty tracking, `baseline` memo, explicit `dirty` boolean, `Cancel` that resets, and a re-seed effect
on revision change all copy `source-configuration-editor.tsx`. Validation runs on blur and on submit,
never per keystroke, and renders through `SettingsField`, which wires `aria-invalid`,
`aria-describedby` and `role="alert"`.

Unsaved-changes guard: the App Router has **no** navigation-blocking API, so interception is at the
link. Two layers — `beforeunload` for leaving the document, and `confirmLeave()` called from every
`<Link>` and back-link `onClick` with `preventDefault()` on refusal. The dirty flag is published from
the panel to the shell through `UnsavedGuardContext`.

**Header dot fallback chain** (undefined today, must be specified):
`display_name` → `notify_email` → `"?"` for the initial; `title` becomes `display_name` when set, else
`notify_email`; before `me` resolves, the dot renders an empty circle with `aria-hidden` and the
trigger is disabled — never a flashing placeholder letter.

**Status branches:** 400 → server `detail`; 403 → "Your session could not be verified. Reload and try
again."; 409 → "Your profile changed on another device. We reloaded it — review your edits and save
again." + `refreshMe()`; 410/423 → "This account is being deleted." + redirect; 429 → `Retry-After`;
503 → "Settings are briefly unavailable. Your changes were not saved."

**Load failure** on `/settings/*` needs its own branch: `bootstrapSession` failing renders a
full-panel error with a Retry button, not a permanent spinner. No session at all (fresh browser,
local-demo `localStorage` empty; or deployment 401) redirects to `/` rather than rendering
`<Onboarding/>` inside the settings shell.

---

## 6. The other settings sections

### 6.1 Interests — `/settings/taste` (v1, free)

The 8 chips from the static shell as `aria-pressed` toggles (`live music`, `art`, `technology`,
`comedy`, `outdoors`, `food`, `community`, `wellness`) plus bounded free entry. **Surfaces
`PUT /v1/preferences`, which exists and has zero callers in `web/`**, while `Me.interests` is already
fetched and rendered nowhere.

Three contract details or the UI 409/422-loops. (a) It is a **whole-profile REPLACE** — the server
rewrites `explicit_affinities` as `dict.fromkeys(interests, 1.0)`; never send a partial list expecting
a merge, and per-interest weights are discarded. (b) The revision is **caller-minted and monotonic**:
the client must send `me.preference_revision + 1` and adopt the returned value — echoing the current
revision back 422s on a fresh tenant and 409s forever after. (c) Mirror `normalize_interests`
client-side (≤20 items, ≤64 chars, whitespace-collapsed, casefolded, unique, no codepoint < 0x20) so
the user sees an inline error rather than a 422. The response is `PreferencesOut`, **not** `Me`;
`status === "replayed"` is success.

Not here: "what we've learned about you" (implicit affinities are merged at read time and never
persisted back) and "reset learned taste" (`tenant_ranking_feedback_receipts` is `SELECT, INSERT` only
by design; a reset needs a watermark plus a `since` parameter threaded through the repository and its
SQL-side aggregate).

### 6.2 Account and data — `/settings/account` (v1)

**(a) Sign-in.** Auth mode; "You'll be asked to sign in again every 8 hours" — the session TTL is
**fixed-lifetime, not sliding** (`mark_recent_auth` deliberately preserves PTTL with `'PX', ttl,
'XX'`); "Destructive actions require a fresh sign-in." Plus `account_management_url` (a new
`UiConfigOut` field — a URL, not user data, so legal on the unauthenticated endpoint) to deep-link an
IdP console the moment one exists. Password, MFA, recovery and linked identities are entirely the
IdP's, and no IdP is configured anywhere today.

**(b) How it works** (slice 6, read-only, replacing three would-be routes):
- *Notifications* — the delivery address plus the kinds actually emitted (`COMPLETION`,
  `HANDOFF_AVAILABLE/REMINDER/REVIEW_REQUIRED/EXPIRED`, `NO_RESULT`, `RECONCILE`) and "Email is the
  only channel today." `DIGEST` and `ATTENDANCE_NUDGE` are declared enum members no code path emits.
- *Autonomy* — the static shell's truthful three-step ladder (preview → durable request → reconciled
  calendar) plus one hard fact: **paid registration is globally refused** at
  `application/registration.py` ("launch has no purchase authority").
- *Connections* — two rows from two `fn_resolve_registration_consent(source, modality)` calls with the
  only two representable tuples, `(meetup, api)` and `(luma, browser)` at `scope='registration'`.
  **No `granted_at`** — the function returns `uuid` and nothing else, and all table grants on
  `tenant_source_consents` are revoked from `ec_app`, so the timestamp is unreadable by any route.
  Plus a read-only calendar-binding row (`GET /v1/me/calendar` — `{connected, write_calendar_id,
  free_busy_calendar_count}`, never a token).

**(c) Danger Zone** — a visually separate card, per ADR-012, not its own route.

Erasure dialog: exact literal `DELETE MY ACCOUNT`, submit disabled until it matches, validated
client-side **and** server-side by `Literal["DELETE MY ACCOUNT"]`. The body is `{request_id: UUID,
confirmation}` under `extra="forbid"` — `request_id` is **required**, minted with
`crypto.randomUUID()` and persisted per-tenant in `sessionStorage` so a retry replays the same id
rather than 409ing. The response is the full `AccountErasureOut`, not a two-field receipt;
`Retry-After` is a *header*.

Branches: **202** → render the *"underway"* receipt from the returned counts — **never "complete"**;
**200** → completed receipt; **409** → "an erasure is already underway"; **428** → "Sign in again to
continue" → `POST config.reauth_url` → `location.assign(authorization_url)`. On any 200/202 the client
must call `clearSession()` **and** `clearCatalogCache()` before rendering — the server clears cookies
but cannot touch `localStorage`, so in local-demo the tenant UUID and its tenant-keyed catalog cache
would otherwise survive an accepted erasure and the next visitor to that browser would resume a
deleted account. There is **no progress poll by design** and no cancel (the fence key is permanent).

### 6.3 Activity — `/settings/activity` (slice 4)

For a product that autonomously registers on your behalf, the Next tree offers no way to see what it
did. Four finished endpoints, zero callers *in `web/`* (the static shell calls all of them).

| Section | Read | Payload |
|---|---|---|
| What I asked for | `GET /v1/requests?cursor&limit` | `RequestSummaryOut {request_id, text, state, created_at, categories, free_only, window_start, window_end, outcome}` |
| What it registered | `GET /v1/registrations?cursor&limit` | `RegistrationSummaryOut {canonical_event_id, title, start_at, end_at, venue_name, city, description, price_status, event_status, state, lane, source, conflict_warning, registration_url, updated_at, can_withdraw}` |
| What needs me | `GET /v1/tasks?state=actionable` | `TaskSummaryOut {task_id, canonical_event_id, event_summary, title, start_at, venue_name, city, reason, state, deep_link, expires_at, created_at}` |

Field names are load-bearing: it is `state`, not `status`; there is **no registration id on the wire**
— withdrawal keys on `canonical_event_id`. `UnrsvpBody` is `{canonical_event_id, request_id}` under
`extra="forbid"` returning 202, and 409s unless the lifecycle is SCHEDULED/RECONCILED/WITHDRAWING —
render that as "retry shortly," not an error. Paging uses the existing opaque cursor with server-capped
`limit ≤ 50`, separate `loading`/`loadingMore`/`error` booleans, and the monotonic `useRef` generation
counter to drop stale responses.

**Not a raw audit dump.** `RegistrationActionAuditPort` exposes only `append()` — no list/query method
exists — and rows are deliberately PII-free. These three projections *are* the sanctioned activity log,
and FR-19.3 fixes what an authenticated read may expose.

**Gap to note:** FR-11.8 requires confirm/decline of the T-24h nudge and self-reported
attended/no-show. This page has read + withdraw and no attendance control. See OQ 5.

### 6.4 Security — `/settings/security` (slice 7)

"Sign out everywhere" is real value with one trap that must be written down loudly.
`revoke_tenant_sessions` looks exactly like the primitive to reach for, and it is not: its Lua script
writes a **permanent, no-TTL** fence key and `create_session` returns `-1` forever after. There is no
un-fence path anywhere in the codebase. Wiring it naively **permanently bricks the account**,
recoverable only by manual Redis surgery. It needs a separately *named* port method
`revoke_all_sessions(tenant_id)` with its own script — not a `fence: bool` flag, so no future caller
reaches the fencing behavior by passing a default — plus the same name in `_BROWSER_SESSION_METHODS`
or production preflight stops enforcing the surface on injected adapters. **Regression test:** after
the new endpoint, assert the `…:erased` fence key does not exist and a subsequent login succeeds.

An active-sessions *device list* is a bigger lift: the Redis index stores only SHA-256 digests in a SET
capped at 32, so it needs the session record extended (with a separately-minted `session_ref`, never
the digest — publishing key material is a privilege-escalation surface), a `user_agent_class` closed
vocabulary rather than the raw UA, a truncated `ip_prefix`, and a `last_seen_at` write preserving PTTL.
Ship "sign out everywhere" first; the list is optional.

### 6.5 Autonomy, spend, billing — not in scope, and why

Every proposed spend cap has nothing to enforce against. A `spend_period` is uncomputable — there is no
spend ledger, no currency column, no amount column, and no payment instrument in 154 migrations;
`provider_budget_*` is a **tenant-neutral** API-call quota with no `tenant_id`. HARD scope decision 4,
FR-7.5 and AC-53 defer paid purchase entirely. `PolicyLimits(rsvps_per_period=20,
concurrent_open_cap=10)` is **dead code** — `adapters/policy/engine.py` is the only occurrence of
`_limits` in the repository. D10 (the per-lane autonomy dial) is HELD, with `design/requirements.md:314`
stating D10-dependent constructs are "deliberately excluded until D10 is ruled"; 0 of 40 owner gates
are signed.

The governing rule: **ship the consent record and the enforcement point together, or ship neither.** A
settings page displaying a spend cap the engine does not read is a safety lie about money — strictly
worse than no page, because it manufactures false assurance.

"Pause my concierge" is equally tempting and equally unavailable: `tenant_policy_control.kill_switch`
is the one per-tenant knob and its three mutation functions have `EXECUTE` revoked from PUBLIC and
never granted to `ec_app`, explicitly so the application "cannot turn off a quarantine or freeze
itself." A user-facing pause needs a *new, narrow, tenant-writable* control plus an ADR-004 review —
never a grant of `fn_set_tenant_policy_kill_switch`.

---

## 7. Data model

### 7.1 Storage decision

**A new satellite table, `public.tenant_profiles`, shaped on `0060_tenant_ranking_profiles`.
`public.tenants` is not altered at all — no new column, no grant change.** `notify_email` stays where
it is and remains the single source of truth; `tenant_profiles` has no email column of any kind.

- **Columns on `public.tenants`** is possible as DDL but not as a *writable* surface. `0061` does
  `REVOKE ALL … FROM ec_app` then `GRANT SELECT`, and
  `tests/integration/test_tenant_identity_rls.py` asserts INSERT/UPDATE/DELETE each raise
  `permission denied`. `tenants` is insert-once identity: `fn_provision_tenant` takes a fixed
  `(uuid, text, text, text)` signature under three deterministic advisory locks. Adding mutable
  columns means either widening a signature that exists specifically to be un-widenable, or admitting
  NULL-defaulted columns provisioning cannot set. Under the satellite design that test stays
  byte-for-byte unchanged and still passes.
- **`GRANT UPDATE (col)`** is rejected on three grounds: **zero column-level-GRANT precedent** in 154
  migrations; it falsifies the load-bearing `0061` comment; and decisively, a column grant is *static*
  authority whereas any identity field's requirement is conditional ("may change only after control is
  proven"). The right primitive for a conditional write is a function, not a grant.

### 7.2 Migration

`revision = "0155"`, `down_revision = "0154"`. **Re-derive with `uv run alembic heads` at
implementation time** — 0154 landed the same day this was written and is still untracked. If several
slices land DDL they must serialize 0155, 0156, …; Alembic refuses multiple heads and the integration
harness runs `alembic upgrade head` before *every* integration test, so a duplicate revision fails the
entire suite.

```sql
CREATE TABLE public.tenant_profiles (
    tenant_id     uuid PRIMARY KEY
                  REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
    display_name  text,
    time_zone     text,
    revision      bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT tenant_profiles_display_name_valid CHECK (
        display_name IS NULL
        OR (display_name = btrim(display_name)
            AND char_length(display_name) BETWEEN 1 AND 64
            AND display_name !~ '[[:cntrl:]]')
    ),
    CONSTRAINT tenant_profiles_time_zone_valid CHECK (
        time_zone IS NULL OR time_zone ~ '^[A-Za-z0-9+_/-]{1,64}$'
    )
);
```

Both new tables take the house RLS posture: `ENABLE` + `FORCE ROW LEVEL SECURITY`, one policy
`USING`/`WITH CHECK` on `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`,
`REVOKE ALL … FROM PUBLIC`, and explicit `SELECT, INSERT, UPDATE, DELETE` to `ec_app`. `ON DELETE
CASCADE` from `tenants` is what makes erasure coverage automatic — but the erasure enumeration and its
`_FENCED_TABLES` list (26 entries; no tenant-bearing table has been created since 0108) must be
extended and asserted, not assumed.

### 7.3 Concurrency

`revision` is caller-minted and monotonic, identical to `PreferencesBody` — one idiom across the whole
account surface. `UPDATE … WHERE revision < EXCLUDED.revision`, 409 on stale.

---

## 8. API

| Method + path | Dependency | Response | Slice |
|---|---|---|---|
| `GET /v1/me` | `AuthenticatedTenant` | `MeOut` (extended) | v1 |
| `PUT /v1/me/profile` | `CsrfProtectedTenant` | `ProfileOut` | v1 |
| `PUT /v1/preferences` *(exists)* | `CsrfProtectedTenant` | `PreferencesOut` | v1 — client only |
| `POST /v1/me/erasure-requests` *(exists)* | `AccountErasureProtectedTenant` | `AccountErasureOut` | v1 — client only |
| `POST /auth/logout` *(exists)* | `CsrfProtectedTenant` | 204 | v1 — client only |
| `POST /auth/reauth` *(exists)* | `CsrfProtectedTenant` | `{authorization_url}` | v1 — client only |
| `GET /v1/requests` \| `/v1/registrations` \| `/v1/tasks` *(exist)* | `AuthenticatedTenant` | `*PageOut` | 4 — client only |
| `POST /v1/unrsvp` *(exists)* | `CsrfProtectedTenant` | `UnrsvpAccepted` | 4 — client only |
| `POST` \| `DELETE` \| `GET /v1/me/avatar` | `CsrfProtected` / `CsrfProtected` / `Authenticated` | `AvatarOut` / 204 / `image/webp` | 5 |
| `GET /v1/me/connections` \| `/v1/me/calendar` | `AuthenticatedTenant` | `ConnectionsOut` \| `CalendarBindingOut` | 6 |

**Eight already exist and need only a client.** Naming follows the file's unbroken conventions:
`<Noun>Body` / `<Noun>Out` / `<Noun>Accepted`, snake_case handlers, no `operation_id`. New routes take
`tags=["account"]`.

```python
class ProfileOut(BaseModel):
    """Mutable, user-authored display facts. Never an identity binding (FR-1.5, ADR-011)."""
    display_name: str | None = None
    time_zone: str | None = None
    avatar_url: str | None = None
    revision: int = 0
    updated_at: str | None = None


class MeOut(BaseModel):
    # shipped fields; order, names and types are frozen
    notify_email: str
    interests: list[str]
    preference_revision: int
    local_demo: bool
    # additive; every one optional or defaulted, so web/lib/types.ts keeps typechecking
    # and the legacy static shell (which reads only notify_email/interests) is unaffected
    relay_inbox: str | None = None
    profile: ProfileOut = Field(default_factory=ProfileOut)


class ProfileBody(BaseModel):
    """Whole-row replace with a caller-minted monotonic revision, exactly like PreferencesBody."""
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    display_name: str | None = Field(default=None, max_length=64)
    time_zone: str | None = Field(default=None, max_length=64)
```

Note `web/lib/types.ts` `Me` is **already drifted** — it omits `local_demo`, which ships in `MeOut`.
Fix that in the same change.

**PUT, not PATCH.** A sparse `PATCH` with `If-Match` is unimplementable as specified —
`default=None` + `min_length=1` + `extra="forbid"` cannot express "leave unchanged" versus "clear," and
`If-Match` introduces a second concurrency idiom plus a 428 that collides with the step-up 428 already
returned by `_account_erasure_protected_tenant`. PUT with the existing body-revision/409 contract is
one idiom and matches the only precedent in the file.

**But whole-row replace needs a deploy-skew guard.** The web and api containers deploy independently.
"Every field is always sent; `null` clears" means the first time a field is added, any browser still on
the previous bundle **nulls it on the next save**. Ship a `fields: list[str]` presence list (or a
schema-version sentinel) from day one — it costs nothing now and cannot be retrofitted without
introducing the second idiom PUT was chosen to avoid.

**Validation split:** `Field(...)` bounds produce pydantic 422s; semantic rules run in
`application/profile_validation.py` and return `{detail: str, errors: [{field, code, message}]}`.
**`detail` stays a string** — `web/lib/api.ts` reads exactly `{detail}` and would render a list as
`[object Object]`. The `errors` array is additive and ignored by today's client.

**Auth dependency selection, restated so nobody hand-rolls it:** `AuthenticatedTenant` buys 401/503,
the 410/423 erasure fence, and the 404 not-provisioned check. `CsrfProtectedTenant` composes on top and
adds 403. `AccountErasureProtectedTenant` additionally sees *through* the erasure fence and gates on
`verify_recent_auth` — correct only for erasure, and must not be borrowed. **Every new mutation takes
`CsrfProtectedTenant` and nothing else.**

**Rate limiting is deliberately out of this vertical.** It is a real gap and a separate
auth-hardening change. Note that every IP-keyed bucket is structurally impossible here:
`web/lib/runtime-api-proxy.ts` strips `forwarded` and every `x-forwarded-*` header, so FastAPI sees
only the Next container's socket address and an IP bucket collapses into one global counter. If it
lands later, key capability endpoints on `sha256(token)`, never a client IP.

**Next.js proxy: no change at all.** `web/app/v1/[[...path]]/route.ts` already forwards `cookie`,
`origin` and `X-EC-CSRF` unchanged, re-emits `Set-Cookie` via `getSetCookie()`, streams responses with
upstream headers preserved, and exempts GET/HEAD from the body cap.

---

## 9. Frontend architecture

### 9.1 Routing: real Next routes

`/settings/*` is a real route tree; the hop from `/` is a plain `<a href>` document navigation, exactly
like today's `/admin` hop.

**Against a sixth `ViewName`:** it requires `VIEWS` extended, `HISTORY_VERSION` bumped 5→6 with v5..v1
still accepted, a settings field threaded through `ConsumerHistorySnapshot` / `copySnapshot` /
`consumerHistoryUrl` / `consumerHistorySnapshotFromUrl`, and a 17th `OWNED_QUERY_PARAMETERS` entry —
miss one and `readConsumerHistorySnapshot` returns `null` and the back button silently resets to chat.
It also leaks catalog state into settings URLs, and the sync effect `replaceState`s on every filter
change, rewriting the settings URL underneath the user. Only a real route can export `metadata`.

**The boundary is total, not partial**, which is what makes it safe:

| Tree | Navigation model |
|---|---|
| `/` (`ConciergeApp`) | `window.history` push/replace + `popstate`. **No** `next/link`, **no** `next/navigation`. |
| `/settings/*` | `next/link` + `usePathname`. **No** manual `pushState`. |
| Seam | plain `<a href>` document navigation, both directions |

`ConciergeApp` — which owns the three history effects and the `popstate` listener — is only mounted by
`app/page.tsx`. On `/settings/*` it does not exist, so the Next router has nothing to fight. A static
test (`tests/account-routing-boundary.test.mjs`) asserts `next/link` and `next/navigation` appear
nowhere in `concierge-app.tsx` or `lib/consumer-history.ts`, that `HISTORY_VERSION === 5`, and that
`VIEWS` still has exactly five members — a future agent who "just adds a sixth view" fails there, with
the reason in the test name.

**Cost, stated honestly** (see OQ 4): every `/settings` visit destroys filters, the chat thread, scroll
position and the in-memory agenda cache, and the return trip reloads them. A Next route *group* or an
intercepting/parallel route would keep `metadata` and the URL while preserving the shell. It is not
chosen here because the history model above is the dominant risk, but it is the one alternative worth
re-opening.

### 9.2 The `/app` route (fixes B-3)

Add `web/app/app/page.tsx` rendering `<AppLanding/>`, which reads `window.location.hash` and
`router.replace()`s to an **exact-match allowlist** of settings paths, defaulting to `/`.
`web/lib/return-path.ts` holds the pure logic and mirrors `_safe_return_path`'s rejections (`//`,
backslash, scheme, netloc, control characters, >256 bytes), so no open redirect is reachable. No
backend change, no whitelist widening, no OIDC adapter edit.

### 9.3 State

**A route-scoped client provider for `/settings/*`, sourced from a shared pure bootstrap.**

A server component is impossible, not merely undesirable: in local-demo the tenant is a UUID in
`localStorage` a server component cannot read, and in deployment the session is a `__Host-` cookie a
server component would have to forward to the API origin directly, bypassing the same-origin proxy
ADR-012 exists to enforce. React context does not cross a document navigation, so lifting `me` into a
shared context does not help either.

`/settings/*` gets one `SessionProvider` in its layout that fetches once for the tree and owns the
write-back; `/` keeps its existing prop threading. Both run the **same** `bootstrapSession(deps)` from
`web/lib/session.ts` — a faithful extraction with dependencies injected so the whole decision tree is
testable with no DOM, plus one new line: `setCsrfContract(...)` **before** `getMe` runs, so a mutation
can never race an unset contract. `readableError` moves from module-private in `concierge-app.tsx` to
an exported function in `web/lib/api.ts` — otherwise the provider cannot import it and `tsc --noEmit`,
a CI gate under `strict: true`, fails.

**Phase 1 touches `ConciergeApp` in four places only:** the `AccountMenu` import, the header swap,
`signingOut` state + `handleSignOut`, and the one `setCsrfContract` line. A 1,200-line component is not
refactored in the change that introduces a route tree.

### 9.4 Styling and layout

No CSS framework, no new dependency. Consumer classes go in `web/app/globals.css` using the 16 `:root`
tokens; naming is the house `block__element` / `block--modifier` / `is-*`.

The dropdown panel copies `.filter-combobox__menu`: `position:absolute; top: calc(100% + 8px);
right:0; background: rgba(17,17,18,.99); border: 1px solid var(--line-strong); border-radius: 12px;
box-shadow: 0 22px 58px rgba(0,0,0,.6); padding: 6px;` plus the 160ms entry animation. Note
`globals.css` has **both** a `prefers-reduced-motion: no-preference` block and a `reduce` block — a new
keyframe must be placed inside one, not left to inherit.

The settings shell is a two-column grid (232px sidebar + `minmax(0,1fr)` content capped at 760px) at
≥1021px, 188px at 761–1020px, and a sticky horizontally-scrollable pill strip at ≤760px. **The shell
header must be a sibling above the grid, not a grid item**: a stickily-positioned box is clamped to its
containing block, and a grid item's containing block is its own grid area — a header row sized exactly
`var(--header-height)` has zero slack and simply scrolls away.

One behavior-changing edit, labelled as such: the 760px block sets `body { padding-bottom: 68px }`
purely to clear the fixed `.mobile-nav`, and `/settings` has no bottom bar, so it would inherit 68px of
dead space. Moving it to `.app-shell` fixes that, but `.app-shell` carries `min-height: 100vh` under
global `border-box`, so this **reduces document height by 68px on `/` at ≤760px** — a deliberate change
needing an assertion.

**Mobile is already over budget.** `.mobile-nav` is `grid-template-columns: repeat(4, 1fr)` while
`NAV_ITEMS` has five entries — the bar already wraps to two rows inside a 68px clearance at ≤760px
*today*. Any discussion of promoting Activity to top-level nav starts from "already over," not "one
slot left." Also, at ≤760px `.site-header` collapses to `1fr auto`, so the menu is the *only*
navigation at the viewport where its geometry matters most.

**Three shared primitives** land under `components/settings/`: `ToastRegion` (`role="status"` polite +
auto-dismiss for success; `role="alert"` assertive + persistent for errors, because an unsaved edit is
not something to blink past), `ConfirmDialog` (the app's first modal — `role="dialog"
aria-modal="true"`, a real Tab cycle, Escape restoring focus, scrim click to cancel but not scrim
`pointerdown`, `body` overflow locked, **no portal**), and `SettingsField`.

**Accessibility beyond the menu and dialog:** the Next App Router moves neither focus nor announcement
on client-side route change, so the four `next/link` tab hops in `SettingsShell` are silent to a screen
reader. Add a route-change live region and move focus to the panel `<h1>`. Add a skip link.

---

## 10. Security & safety requirements

**S-1 · CSRF plumbing is a prerequisite** (B-2). Verification is three-factor: the session cookie's
tenant must match the authenticated tenant; `Origin` must `hmac.compare_digest`-equal the origin
derived from `EC_PUBLIC_BASE_URL`; and the CSRF cookie must equal the header **and** its SHA-256 must
equal the `csrf_hash` inside the session record. Corollary for deployment docs: **`EC_PUBLIC_BASE_URL`
must be the public Next origin, not the API's internal origin** — the proxy forwards the browser's
Origin unchanged, so a wrong value 403s every mutation with an error pointing at CSRF rather than config.

**S-2 · `relay_inbox` is never editable and never a form field.** It is UNIQUE, insert-once, and
CI-lint-enforced never to send (ADR-011). If it were editable a tenant could point theirs at another
tenant's relay address and receive that tenant's inbound OTP and magic-link mail — a direct
cross-tenant credential-theft primitive. Displaying it **to its own owner** is safe but widens the
authenticated-read projection FR-19.3 governs, so it needs a recorded acceptance criterion (OQ 3).

**S-3 · `notify_email` change needs the four prerequisites in §5.3, and B-1 resolved first.** The
verification link must be a **GET that renders a form and changes nothing, POST that commits** — the
split `_show_handoff_done` / `_mark_handoff_done` already models it — so a mail scanner prefetching the
URL cannot silently commit a channel change. Two emails on request: the verification link to the new
address, and a non-suppressible security notice to the current one, which is the actual takeover
defence. **Do not add `UNIQUE` on `notify_email`** — it has none today, identity is `oidc_subject`, and
a friendly "that email is already in use" is a pure account-enumeration oracle contradicting
`fn_provision_tenant`'s deliberate single-generic-conflict posture.

**S-4 · Step-up must not be reused as-is for any second sensitive action.** `_REAUTH_PURPOSE` is a
module constant, `verify_recent_auth` never reads a purpose and never clears `recent_auth_at`, so for
the whole window an erasure step-up satisfies *any* step-up gate. **This is the highest-value new test
to write, and it fails today.** Any new step-up dependency must **not** copy
`_account_erasure_protected_tenant`'s erasure-fence bypass, and must carry the `if not
settings.mock_cloud:` guard or it returns 503 in the only mode the product currently runs. A version
bump of the session record is *not* the way to introduce a purpose: `_STORE_VERSION` is one constant
shared by the login-transaction payload and the session record, and `_session_record` hard-rejects a
mismatched `v`, so bumping it signs out every live user and fails every in-flight login. Add `grant`
to the existing **field-set allowlist** — that tolerance is the codebase's actual back-compat idiom —
and treat any record without it as satisfying no purpose.

**S-5 · Avatar upload** (§5.4): raw body, not multipart; magic bytes only; dimensions bounded *before*
`img.load()`; `Image.MAX_IMAGE_PIXELS` set at import; multi-frame rejected; **full decode and re-encode
from a fresh canvas** — never store the uploaded bytes; server-generated content-addressed identity;
`image/svg+xml` rejected structurally, never "sanitized." Pillow must be reviewed as security code and
enter the `pip-audit` loop. There is **no `avatar_url` fetch, no Gravatar, no OIDC `picture` proxying**
— the API pod runs under Workload Identity, and an SSRF reaching `169.254.169.254` mints tokens for a
service account holding `roles/storage.objectAdmin` and `roles/cloudsql.client`. That is credential
exfiltration, not "read an internal page."

**S-6 · Serving is self-only and never shared-cacheable.** No `/v1/tenants/{id}/avatar`.
`private, max-age=0, must-revalidate` + `Vary: Cookie`; never `Access-Control-Allow-Origin`. A
per-session-varying URL behind any shared cache serves tenant A's face to tenant B, and the Next proxy
passes upstream response headers through verbatim.

**S-7 · "Sign out everywhere" must never call `revoke_tenant_sessions`** (§6.4).

**S-8 · Settings as consent for autonomous spend.** Any future authority-increasing write must be
recorded in the *same transaction* as its consent evidence, with a `CHECK (direction <> 'raise' OR
reauth_grant_id IS NOT NULL)` so an unattested raise is structurally impossible; the grant must be
bound to a digest of the exact proposed diff, not merely a time window; and the limit must be
**re-read at the last fence** inside `TenantEffectAuthority.run(...)` — never cached in workflow state
— so a lowering binds mid-flight. A raising must never apply retroactively.
`application/registration.py` already implements exactly this discipline for policy and says so in a
comment; copy it. None of this ships in this vertical — it is written down so the slice that does
ships it correctly.

**S-9 · De-escalation is never step-up-gated.** Sign-out, sign-out-everywhere, revoking a source,
lowering a cap. Gating the incident-response button makes it harder to press for a victim who may
already be sharing their account with an attacker.

**S-10 · Logging.** Extend `_SENSITIVE_KEY_MARKERS` in `infra/logging.py` with `displayname`,
`relayinbox`, `avatar`, `timezone`, `useragent`, `ipaddress`. The matcher case-folds and strips
non-alphanumerics, so `displayname` catches `display_name` and `displayName`. Note **`relay_inbox` is
not redacted today** — no marker substring matches it. Metrics need no change. There is **no
distributed tracing in this repository** — no OTel dependency, no tracer, no span — so any claim that
"PII is excluded from traces" describes something that does not exist.

**S-11 · CSP for the Next tree** (B-5).

**S-12 · AC-101** names "Settings" in the committed Playwright gate and today drives the *static
shell's* `#/settings`. It must be repointed at the Next route or this vertical ships with no browser
gate at all.

---

## 11. Build order

**Slice 0 — unblock (defect fixes, no feature).**
0. **B-1: deployment tenant provisioning.** Decide and implement the authenticated-path call to
   `tenant_repo.add` / `fn_provision_tenant` (and what supplies `notify_email` — see OQ 1).
1. B-2: CSRF plumbing in `web/lib/api.ts`, `readableError` exported, `setCsrfContract` wired into
   `ConciergeApp`'s bootstrap; regression test over **all** non-safe callers. Document
   `EC_PUBLIC_BASE_URL` = public Next origin.
2. B-3: `web/app/app/page.tsx` + `web/lib/return-path.ts`.
3. B-4: `test:account` in `web/package.json` **and** `.github/workflows/ci.yml`.

**Slice 1 — v1 (the owner's literal ask).** ◀ **THE v1 SLICE**
4. `AccountMenu` + header swap + `handleSignOut` (incl. the 503 branch and the preserved local-demo
   `/admin` link), `SettingsShell`, `SessionProvider`, `web/lib/session.ts`, the `/settings` route tree,
   the three shared primitives.
5. Migration 0155 `tenant_profiles` + RLS posture test + `ports/tenant_profile.py` + Postgres adapter +
   `PUT /v1/me/profile` + the `MeOut` / `ConsumerIdentity` / `ConsumerReadPort` / `web/lib/types.ts`
   extension (all four change together; fix the `local_demo` drift here).
6. `/settings` Profile editor — display name, time zone, read-only email + relay inbox, initials tile.
7. `/settings/taste` against the existing `PUT /v1/preferences`.
8. `/settings/account` — sign-in facts + Danger Zone.

Satisfies the ask: dropdown ✓, edit profile persisted ✓, other settings ✓, API ✓ — **except the photo,
which is slice 5** (OQ 2).

**Slice 2 — ADR-013.** Supersedes ADR-012's consumer-shell clause (it forbids "no Node dependency,
bundler, or independent client deployment," already contradicted in-tree by `web/` + its Dockerfile
with no decision record), re-affirms its same-origin / CSRF / erasure invariants, declares
`api/static/` frozen, records a **parity matrix and cutover plan** for the shipped `#/settings`,
`#/plans`, `#/tasks` fragments, and repoints AC-101. **This should gate slice 1, not run parallel to
it** — until it lands, two consumer shells are in dual maintenance with no rule about which wins.

**Slice 3 —** Next security headers (B-5).
**Slice 4 —** `/settings/activity`. Four finished endpoints, zero callers. Add a test asserting TS
field names against the OpenAPI schema so type drift fails in CI.
**Slice 5 —** avatar (migration, Pillow, `application/profile_media.py`, three routes, `AvatarPicker` +
pure crop math in `web/lib/avatar-image.ts`), with `assert.ok(AVATAR_MAX_BYTES < 64 * 1024)` so a
future edit that raises the ceiling past the proxy cap fails in a test rather than in production.
**Slice 6 —** read-only "How it works" cards + `account_management_url`.
**Slice 7 —** fence-free `revoke_all_sessions` + `/settings/security`.
**Slice 8 —** email change, only after S-3's prerequisites.

**Blocked, not scheduled:** source credential capture (G3 open, no credentials table,
`_build_credential_vault` refuses to construct when `mock_cloud` is false); calendar OAuth (port
explicitly defers it; scope-locked and lint-gated); notification-preference storage (only alongside the
emitter — a quiet-hours setting the notifier ignores is a lie); autonomy dial (D10 held); spend limits
and billing (no money model exists).

---

## 12. Open questions for the owner

1. **B-1 first.** Deployment has no account-creation path, so no deployment account can have a
   `notify_email` to display. **Either** fix provisioning as slice 0 item 0 and let that decide whether
   the address is IdP-derived (read-only, clean) or user-supplied at onboarding (needs the full change
   flow) — **recommended** — **or** rule that this vertical targets local-demo only for now and say so
   explicitly in the doc.

2. **Photo in v1?** The ask said "name, email, photo." As scoped, v1 ships an initials tile and the
   upload lands in slice 5. **Either** accept that (**recommended** — a permanently disabled Upload
   button is worse than its absence), **or** pull slice 5 into v1, which adds Pillow, a second
   migration and the whole §5.4 attack surface to the first shippable slice.

3. **`relay_inbox` on `MeOut`.** An FR-1.5 user-facing concept the user cannot see anywhere today.
   **Either** add it read-only with a new acceptance criterion recording the disclosure under FR-19.3
   (**recommended**), **or** leave it hidden and drop the Profile row.

4. **Settings as a separate document tree.** Every `/settings` visit tears down the SPA (filters, chat
   thread, scroll, agenda cache). **Either** accept that for the safety of a total routing boundary
   (**recommended**), **or** spend a round evaluating a Next route group / intercepting route that
   keeps `metadata` and the URL while preserving the shell.

5. **Three signed requirements this design defers.** FR-15.3/AC-89 (stored quiet-hours, digest
   frequency, channel choice), FR-11.8 (confirm/decline the T-24h nudge, self-report attended/no-show),
   and FR-2.11/2.12 + AC-14/15 (single-source disconnect, `needs_reauth` re-consent). Note G3 gates
   **FR-2.13 capture only** — disconnect and revocation-health are *not* G3-blocked, so the page map's
   "blocked (G3)" is wrong for two of three. **Either** confirm they stay deferred with the reasons
   above recorded, **or** pull disconnect/re-consent into slice 6, which is genuinely available now.

6. **Data export.** You are shipping the erasure right without its access counterpart. **Either**
   accept the asymmetry for now (**recommended** — export is a whole vertical: leased worker,
   claim-check store, one-shot capability URL), **or** schedule it, in which case it is its own design.

7. **`time_zone` fails this design's own cut test.** Its claimed readers are a per-request SQL argument
   and two unemitted notification kinds — while a real reader already exists and is browser-derived
   (`calendarTimeZone()` feeding `getCatalogSummary`). **Either** cut it from v1 alongside `locale` and
   `home_city` (**recommended** for consistency), **or** keep it and rule now that the stored zone
   overrides the browser one, so it does not become a silent second source of truth.

8. **The nine unprotected tenant tables.** `outbox`, `notification_ledger`, `watch_subscriptions`,
   `handoff_expiry_queue`, `request_start_outbox`, `event_change_deliveries`,
   `event_change_calendar_repairs`, `lifecycle_organizer_change_ledger`,
   `lifecycle_watch_projection_outbox` carry `tenant_id` with RLS neither enabled nor forced and zero
   policies. **Either** confirm they are intentionally cross-tenant worker queues and allowlist them in
   the new posture test with the reason in a comment (**recommended**), **or** treat it as a real
   pre-existing gap — its own change, predating this vertical.

Also not decided here, and owner-scope rather than engineering: freezing `api/static/` (slice 2)
retires the only currently-shipped consumer surface.
