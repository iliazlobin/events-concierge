# Calendar OAuth Verification and the CASA Tier 2 Launch Gate

> Closes the post-wave-1 gap on what actually gates public launch of calendar-write across Google/Microsoft/Apple: confirms a calendar-only Google scope set stays in the SENSITIVE tier and avoids CASA Tier 2 entirely, while still forcing a real multi-week app-verification schedule item onto the critical path.

## Verified findings

### Google: calendar-only avoids CASA Tier 2 [confirmed]
A calendar-only Google app (no Gmail/Drive/Chat/Photos scopes) does not trigger CASA (Cloud Application Security Assessment / App Defense Alliance) Tier 2 — no third-party DAST scan, no annual recertification, no Letter of Validation.

- Google's authoritative restricted-scope list (support.google.com/cloud/answer/13464325) enumerates restricted scopes for exactly seven APIs: Gmail (7 scopes), Drive (8), Fitness (22), Chat (5), Data Portability (16), Photos Ambient (2), Health (15). Calendar is absent — so `calendar.events`, `calendar.freebusy`, `calendar.app.created` are sensitive-tier at most.
- The restricted-scope-verification doc states the independent security assessment is triggered only for apps that "request access to Google users' RESTRICTED data and have the ability to access data from or through a third-party server." Calendar is not restricted, so the assessment is never triggered.
- The sensitive-scope-verification doc lists sensitive-scope requirements (policy compliance, branding, domain verification, consent-screen accuracy, per-scope justification, demo video) and makes NO mention of CASA, DAST, or a Letter of Validation.

CAUTION (does not refute the claim): avoiding CASA is NOT avoiding verification. Several third-party security-vendor blogs (DeepStrike, Orbis, Leviathan) market "CASA Tier 2 for sensitive APIs such as reading calendar," conflating the App Defense Alliance tier structure with Google's actual trigger; these are self-promotional and contradicted by Google's own docs. A sensitive-scope calendar-write app still completes standard OAuth app verification — a real launch gate, just not a CASA gate.

### Google: calendar.events (write) is SENSITIVE, not restricted [confirmed]
`calendar.events` write requires app verification only. Every element verified against Google primary docs plus current (2026) real-world evidence:

- Tier: Google's sensitive-scope-verification doc names "reading events stored in Google Calendar" as a canonical sensitive-scope example; write is the same tier. A May 2026 Google Developer forum thread shows a live `calendar.events` verification classified by Google as "sensitive scope verification" — tier is current as of mid-2026.
- Requirements: brand/OAuth-consent verification (domain ownership via Search Console, accurate consent screen), a per-scope justification ("why a narrower scope isn't sufficient"), and an unlisted YouTube demo video of the grant flow. ("Trust & Safety review" is a fair paraphrase of the doc's "review by Google," not a separately documented step.)
- Timeline: doc states verbatim "The sensitive scope verification process can take up to 10 days to complete." Real-world evidence contradicts the nominal figure — a current Google Developer forum thread is titled "OAuth Verification Under Review for Over 5 Weeks (calendar.events)." Plan 3-6 weeks wall-clock including demo-video prep and reviewer round-trips.
- No CASA for sensitive scopes (confirmed, as above).

### Google: refresh-token model is a hard multi-tenant constraint [confirmed]
Verified verbatim against developers.google.com/identity/protocols/oauth2 (current July 2026):

- "There is currently a limit of 100 refresh tokens per Google Account per OAuth 2.0 client ID."
- "If the limit is reached, creating a new refresh token automatically invalidates the oldest refresh token without warning."
- A separate, higher, unpublished cross-client limit exists ("across all clients").
- Testing publishing status: "a publishing status of 'Testing' is issued a refresh token expiring in 7 days, unless the only OAuth scopes requested are a subset of name, email address, and user profile." Calendar scopes are not in that subset, so Testing-status refresh tokens expire in 7 days — we cannot ship on Testing and must publish to Production (which forces verification).

[adjusted] CORRECTED SCOPING: the 100-token limit is per YOUR client ID. A user's OTHER third-party apps use their own client IDs and their own separate 100-token buckets — they do NOT eat into our 100. Only the separate higher cross-client limit is account-wide. The researcher's "across our fleet + their other apps sharing behavior" conflated the two independent limits; within a single shared client ID, only our own re-authorizations count against the 100. The engineering mitigation is unchanged: store exactly one long-lived refresh token per user, never re-prompt.

### Google: incremental auth + calendar.app.created are the minimization tactics [confirmed]
- `calendar.app.created` is real and does exactly what is claimed. Google's "Choose Google Calendar API scopes" page defines it: "Make secondary Google calendars, and see, create, change, and delete events on them." This is the app-owned dedicated-secondary-calendar pattern; it does not touch the user's primary calendar or all events. The same page endorses minimization: "you should choose the most narrowly focused scope possible."
- Incremental authorization is confirmed on the web-server OAuth guide: "Google's authorization server supports incremental authorization... request scopes as they are needed," via `include_granted_scopes=true`, with "request authorization for resources at the time you need them" called a best practice. Supports requesting calendar write only at the scheduled-to-calendar step, not at signup.
- [adjusted] The "four new OAuth scopes... limit access to only the data you really need" quote is real but dates to an Oct 31, 2018 Calendar API release note — these granular scopes are ~8 years old, not recently added. Drop "newer"; they remain the correct narrower scopes and none are deprecated.
- [unverifiable] The exact sensitivity label of `calendar.app.created` vs `calendar.events` could not be confirmed to primary-source certainty (Cloud Console shows categories automatically rather than enumerating them in docs). `calendar.events` is definitively sensitive; treat `calendar.app.created` as sensitive-tier-at-most and confirm in-console at build time.

### Multi-provider CalendarPort: Microsoft and Apple gates (researcher confidence: high; non-load-bearing)
- Microsoft Graph: gate is Publisher Verification (a blue-check on the consent prompt; without it enterprise/professional accounts see an "unverified publisher" warning). No CASA/DAST equivalent for Graph calendar. Sync uses delta query (`GET /me/calendarView/delta?startDateTime=…&endDateTime=…`) returning added/updated/deleted events, paired with change-notification webhooks. Subscription write ops (POST/PUT/PATCH/DELETE) are capped at 500 requests per 20 seconds per app per tenant; the `notificationUrl` must echo Microsoft's `validationToken` as plain text within 10 seconds on subscription creation.
- Apple iCloud: NO developer portal, NO SDK, NO calendar API, NO OAuth, NO formal verification. Only path is CalDAV at `caldav.icloud.com` with a user-generated app-specific password (Basic auth, requires 2FA on the account); writing an event means PUTting a whole `.ics` file. Best-effort adapter with per-user manual credential entry, off any reliability SLA.

### Budget guardrail: restricted-scope creep cost (researcher confidence: medium; non-load-bearing, secondary sources)
Third-party assessor pricing (TAC Security via DeepStrike): CASA Tier 2 ~$540-$1,800/year, Tier 3 ~$4,500; Tier 2 lab testing ~1-3 weeks once testing begins (excluding remediation); mandatory 12-month recertification. As of 2025-2026 the Tier 2 SELF-scan option was removed — restricted-scope apps must engage an authorized third-party assessor. This is a guardrail, not a launch cost: staying calendar-only avoids all of it. Any future Gmail/Drive-based event ingestion would flip the whole OAuth client into restricted verification plus recurring CASA cost and multi-week annual DAST.

## Design implications

Directives for the Events Concierge:

1. **Google-only sensitive-scope launch plan.** Request `calendar.events` (write) + `calendar.freebusy` (dedup) and NEVER add any Gmail/Drive/Chat/Photos scope — that boundary keeps the app off CASA Tier 2. Enforce as a hard architectural rule / lint on the CalendarPort Google adapter: it may only ever hold calendar-family scopes.

2. **OAuth verification is on the critical path, pre-launch.** Sequence: brand/consent verification (2-3 business days) then sensitive-scope review (nominal "up to 10 days," plan 3-6 weeks wall-clock). It gates leaving Testing status; submit before public launch, not as a fast-follow.

3. **Never ship on Testing status.** Refresh tokens expire in 7 days there for calendar scopes. Publishing to Production is mandatory and is what forces verification — make "submit for verification" a pre-launch milestone.

4. **Credential vault: one refresh token per user, treated as durable root secret.** Never re-run the consent flow for an already-connected user (churns toward the 100-token silent-invalidation cap and causes `invalid_grant`). Handle `invalid_grant` via a single explicit re-consent and monitor it as a first-class lifecycle error in the Temporal workflow. The 100 cap is per-our-client-ID only; our own re-auth behavior is the sole thing that consumes it.

5. **Scope minimization as security AND approval strategy.** Prefer writing concierge events to a dedicated app-managed secondary calendar (`calendar.app.created` pattern) and use incremental authorization (`include_granted_scopes=true`) — request calendar write only at the scheduled-to-calendar step, after discovery/RSVP. Keeps signup consent lightweight and cleanly answers the reviewer's "why a narrower scope isn't sufficient." Confirm `calendar.app.created`'s category in Cloud Console before relying on it staying sub-restricted.

6. **Per-provider verification matrix for CalendarPort:**

| Provider | Launch gate | CASA/DAST | Sync mechanism | Hard limits | Credential model |
|---|---|---|---|---|---|
| Google | Sensitive-scope app verification (schedule gate, plan 3-6 wk) | None (calendar-only) | Push notifications + events.list sync tokens | 100 refresh tokens/account/client; 7-day token expiry on Testing | OAuth refresh token (one per user) |
| Microsoft Graph | Publisher Verification (blue-check; avoids "unverified publisher") | None | delta query + change-notification webhooks | 500 writes/20s/app/tenant; validationToken echo <10s | OAuth |
| Apple iCloud | None (no portal/API/verification) | N/A | CalDAV polling; whole-.ics PUT | Off reliability SLA; best-effort only | App-specific password (per-user manual entry, Basic auth) |

7. **Bright red roadmap line.** Any future "discover events from the user's inbox" (Gmail) or Drive-based feature flips the WHOLE OAuth client into restricted-scope verification + recurring CASA Tier 2 (annual DAST, ~1-3 wk lab cycles, ~$540-$1,800/yr, 12-month recert). Keep event discovery on API-first/browser sources so calendar write stays the only Google scope. This is a deliberate decision, never an accidental scope addition.

## Sources

- [Restricted scopes (authoritative list)](https://support.google.com/cloud/answer/13464325) — the only 7 APIs with restricted scopes (Gmail, Drive, Fit, Chat, Data Portability, Photos, Health); Calendar absent, confirming calendar-only avoids CASA.
- [Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) — third-party-server access to restricted data triggers the security assessment; annual recert; brand verification 2-3 business days.
- [Sensitive scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification) — Calendar named as sensitive example; app verification only, no CASA; "up to 10 days"; per-scope justification + unlisted YouTube demo video.
- [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2) — 100 refresh tokens/account/client-ID with silent oldest-token invalidation; 7-day refresh-token expiry on Testing status.
- [OAuth 2.0 for Web Server Applications](https://developers.google.com/identity/protocols/oauth2/web-server) — incremental authorization via `include_granted_scopes=true`; request-at-time-of-need best practice.
- [Choose Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth) — `calendar.app.created` = app-owned secondary calendar; minimization guidance.
- [Google Calendar API release notes](https://developers.google.com/workspace/calendar/release-notes) — Oct 31 2018 "four new OAuth scopes" granular-scope announcement (dates the "newer" scopes).
- [OAuth 2.0 Scopes for Google APIs](https://developers.google.com/identity/protocols/oauth2/scopes) — full list of Calendar scope URIs.
- [Microsoft Graph subscription resource](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0) — 500 writes/20s/app/tenant; validationToken echo <10s.
- [Microsoft Graph event delta](https://learn.microsoft.com/en-us/graph/api/event-delta?view=graph-rest-1.0) — delta query for add/update/delete event sync.
- [How to integrate iCloud Calendar API (OneCal)](https://www.onecal.io/blog/how-to-integrate-icloud-calendar-api-into-your-app) — iCloud has no API/SDK/portal/verification; CalDAV + app-specific password, whole-.ics PUT (secondary source).
- [Sign in with app-specific passwords (Apple)](https://support.apple.com/en-us/102654) — app-specific password mechanism (requires 2FA); the only credential path for third-party iCloud CalDAV.
- [Google CASA 2025: Tiers, Costs & Compliance (DeepStrike)](https://deepstrike.io/blog/google-casa-security-assessment-2025) — Tier 2 ~$540-$1,800/yr, ~1-3 wk lab, 12-month recert; self-scan removed 2025-2026 (secondary source, budget context only).
