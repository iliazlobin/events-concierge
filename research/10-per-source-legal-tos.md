# Per-Source ToS, Automated-Access & CFAA Posture for Free RSVP-on-Behalf

> Closes the post-wave-1 gap on whether — and how — we may act on a user's stored third-party credentials to RSVP. It converts a vague "is scraping legal" worry into a concrete per-source, per-modality policy matrix and pins the real liability vector (contract/tort + account bans, not CFAA crime), which drives the credential-storage, consent-record, and circuit-breaker design.

## Verified findings

### Load-bearing (fact-checked)

**Eventbrite: sanctioned API write-on-behalf; browser automation prohibited. [confirmed]**
Eventbrite's API Terms of Use (last updated May 30, 2025), §4.2, requires "prior, clear, express consent from each Eventbrite User whose Content you access via the Eventbrite APIs" and enumerates that consent as covering the right "(1) to access such User's Eventbrite account(s); (2) to retrieve, store and use Content from such account(s); and (3) to write information to such account(s)," and further that consent be "specific as to each purpose." This is a genuine contractual allowance for consented write-on-behalf — the sanctioned RSVP-on-behalf path. Rate limit is 1000 calls/hour per OAuth token (§3.5). §9 bars disclosing developer credentials to any third party. The consumer Terms of Service §13.1 ("Scraping or Commercial Use of Site Content is Prohibited") states: "You have no right to, and you agree not to, scrape, crawl, or employ any automated means to extract data from the Sites." Net: Eventbrite RSVP is ALLOWED via API-with-OAuth-consent, PROHIBITED via browser automation.

**Meetup: sanctioned 3-legged OAuth RSVP mutation; gated on a paid Pro subscription + audit rights. [confirmed]**
Meetup's GraphQL API supports write mutations; user-specific operations (managing RSVPs, creating events, accessing group memberships) require three-legged OAuth where the member authorizes an access token — first-party consented automation is the designed mechanism. Only members with an active Meetup **Pro** subscription can create the OAuth consumer, and Pro does not guarantee approval (Meetup can deny/revoke licenses). Load-bearing precision from fact-check: only the OAuth-consumer **creator** (our app) needs Pro; end users who authorize the app do **not** need their own Pro — one Pro seat covers a multi-tenant free-RSVP app, each member simply consents via OAuth. The API License Terms let Meetup "monitor or audit your use of the API to verify compliance" and bar selling/leasing/sublicensing (limited, non-exclusive, non-transferable, non-sublicensable, revocable license). Net: Meetup RSVP-on-behalf is ALLOWED via API with per-user OAuth, gated on a Pro subscription and audit compliance.

**Luma: public API is host-side only; user-side RSVP has no API surface → GREY. [confirmed]**
Luma's public REST API (base `https://public-api.luma.com`, auth via `x-luma-api-key` header) exposes only organizer/host operations (get event, list/get/add/update guests, waitlists, ticket types, coupons, webhooks, calendar/org admin). API keys are scoped to a single calendar (the organizer's). There is **no attendee self-registration endpoint** for a user registering on a third party's event. The only host-side write (add/update guest) is the event OWNER adding a guest with the host's key — not our attendee use case. [adjusted] The Luma API is therefore **not purely read-only** (it has host-keyed write endpoints), but none create a first-party path for our scenario. Luma's Terms of Use (Acceptable Use) states: "you must not access the Service by any means other than our publicly supported interfaces" — the sole operative restriction, which a browser bot arguably violates. Net: Luma user-RSVP is GREY — no sanctioned first-party path, browser best-effort with human handoff, off the reliability-SLA critical path.

**Partiful: no public API; ToS bans robots/scraping AND IP-block circumvention → hardest-prohibited. [confirmed]**
Partiful's Terms of Service ("Conditions of Access and Use," last updated December 2, 2025) confirm verbatim: users may not "engage in or use any data mining, robots, scraping, or similar data gathering or extraction methods"; may not "circumvent, remove, alter, deactivate, degrade, or thwart any of the content protections, platform restrictions or geographic restrictions applicable to the Service"; and "If you are blocked by Partiful from accessing the Service (including by blocking your IP address), you agree not to implement any measures to circumvent such blocking (e.g., by masking your IP address or using a proxy IP address or virtual private network)." No official developer/data API exists (only an unofficial reverse-engineered client; the sole official third-party integration is Stripe Connect for ticketing). Net: PROHIBITED / disabled at launch; if shipped, browser best-effort with immediate human handoff and no IP-rotation/anti-block evasion.

**CFAA is a weak theory post-Van Buren; the binding risk is breach-of-contract / trespass-to-chattels + account/IP bans. [confirmed]**
Van Buren v. United States (June 3, 2021) adopted a narrow "gates-up-or-down" CFAA reading: liability attaches only when someone accesses areas "off limits to him," not when an authorized user accesses for a disfavored purpose. A user automating their own logged-in account is inside their own gate, so "exceeds authorized access" is weak. Van Buren expressly **left open** whether contractual/ToS limits (vs. technical barriers like passwords) can define authorization. The hiQ v. LinkedIn saga confirms the risk split: the Ninth Circuit (2019, reaffirmed Apr 2022) held CFAA does not reach scraping of PUBLIC data, but hiQ ultimately LOST on non-CFAA grounds — a Dec 6, 2022 stipulated judgment imposed a $500,000 award and found liability for breach of LinkedIn's User Agreement and California trespass-to-chattels/misappropriation. Translation: ToS violations are enforced via contract + state tort law + account bans, not the CFAA. The operational risk (per-user account suspension, IP blocks against our egress) is the binding constraint, not criminal exposure.

### Supporting (researcher confidence)

**Ticketmaster (Discovery API) and SeatGeek (Platform API) are read-only discovery sources, API-clean. (high confidence)**
Ticketmaster Discovery API allows 5000 calls/day at 5 req/s by default (raised case-by-case after ToS/branding compliance review); terms prohibit selling/leasing/sublicensing or deriving unapproved revenue. SeatGeek Platform Terms require an authorized client ID, run a paid affiliate/partner program, and bar displaying other sellers' listings or claiming endorsement. Neither logs into a user's account or performs RSVP — metadata/discovery only, so no credential-storage or automated-account-action ToS exposure. First-class API-clean, SLA-guaranteed sources.

**GDPR/CCPA exposure is concentrated in STORING the credential, not the RSVP action. (medium confidence)**
Storing users' third-party logins to act on their behalf makes those credentials personal data under GDPR Art. 4 and CCPA, and account-access secrets. The consent Eventbrite §4.2 and Meetup's OAuth flow require **also** serves as the GDPR lawful-basis/consent record — capturing per-source, per-scope consent with timestamps does double duty. OAuth tokens are strongly preferable to stored passwords: scoped, revocable, never expose the raw credential (Eventbrite §9 bars sharing credentials). Argues for token-based flows wherever a sanctioned API exists and a KMS-envelope vault only for residual password-based browser sources.

## Design implications

### Per-source × per-modality launch matrix

| Source | API modality | Browser modality | Launch default | On-behalf write path | Gating / notes |
|---|---|---|---|---|---|
| Meetup | ENABLED | DISABLED | ENABLED via API | 3-legged OAuth RSVP mutation | Requires org-level Meetup **Pro** to own the OAuth consumer; audit compliance; users need no Pro |
| Eventbrite | ENABLED | DISABLED | ENABLED via API | OAuth write-on-behalf (§4.2) | 1000 calls/hr/token; browser prohibited by consumer ToS §13.1 |
| Luma | N/A (host-side only) | GREY | GREY — browser best-effort + human handoff | None (no user-side API) | Off the SLA path; "publicly supported interfaces" clause |
| Partiful | none | PROHIBITED | DISABLED | None | No API; ToS bars robots/scraping + IP-block circumvention |
| Ticketmaster | ENABLED (read-only) | N/A | ENABLED — discovery only | N/A | 5000 calls/day, 5 req/s; no on-behalf action |
| SeatGeek | ENABLED (read-only) | N/A | ENABLED — discovery only | N/A | Client-ID; no on-behalf action |

### Directives

1. **`automation_allowed` is per-source, per-MODALITY, not a boolean.** Encode `{api: enabled/disabled, browser: enabled/disabled}`. The SourcePort adapter-selection must **refuse the browser adapter** for a source whose API is the only ToS-sanctioned modality (Eventbrite, Meetup).
2. **Prefer OAuth tokens over stored passwords** for every source with a sanctioned API (Meetup, Eventbrite, Google Calendar). Reserve the KMS-envelope password vault strictly for browser-only sources (Luma, Partiful) and treat those as the higher-risk, lower-SLA tier.
3. **Build a first-class CONSENT RECORD** — per-source, per-user, per-scope (timestamp; scopes: read / store / write-on-behalf), keyed to the `(user, source)` pair. One artifact satisfies Eventbrite §4.2, Meetup's OAuth authorization, AND the GDPR/CCPA lawful-basis requirement for storing the credential.
4. **Design compliance around contract/tort + bans, not CFAA.** Add per-source, per-user rate-limiting, human-cadence pacing, and a **circuit-breaker that quarantines a source on ban/403 signals** rather than retrying. NEVER implement IP-rotation or block-circumvention — Partiful's ToS makes circumvention an independent violation.
5. **Isolate ban blast-radius.** Browser-only sources must egress through per-tenant or rotating-but-attributable identities so one user's suspension cannot cascade to the whole service. A per-user account ban must fail that user's workflow gracefully into human handoff, not silently retry.
6. **Policy-gate before the register activity in the durable workflow.** Evaluate `automation_allowed` BEFORE the register activity runs, so a source flipped to disabled (ToS change / ban wave) short-circuits in-flight `(user, event)` workflows to a human-handoff state rather than attempting a now-prohibited action.
7. **Separate audited action types for the `found → registered` transition.** The `registered` transition carries different liability and evidentiary needs depending on OAuth-consented API vs. best-effort browser. Log which modality performed each RSVP.

## Sources

- [Eventbrite API Terms of Use (updated May 30, 2025)](https://www.eventbrite.com/help/en-us/articles/833731/eventbrite-api-terms-of-use/) — PRIMARY. §4.2 consent to write on behalf (verbatim confirmed); §3.5 1000 calls/hr/token; §9 no credential sharing.
- [Eventbrite Terms of Service](https://www.eventbrite.com/help/en-us/articles/251210/eventbrite-terms-of-service/) — PRIMARY. §13.1 bans scrape/crawl/automated extraction → RSVP must go via API, not browser.
- [Meetup API Authentication (GraphQL)](https://www.meetup.com/graphql/authentication/) — PRIMARY. Three-legged OAuth server flow; RSVP mutations authorized by member OAuth = sanctioned on-behalf path.
- [Meetup API License Terms](https://help.meetup.com/hc/en-us/articles/360028705532-Meetup-API-license-terms) — Monitor/audit rights; no resale/sublicense; revocable license.
- [How can I get access to Meetup's API](https://help.meetup.com/hc/en-us/articles/41453576628749-How-can-I-get-access-to-Meetup-s-API) — OAuth consumer creation requires active Meetup Pro (creator only, not end users).
- [Luma Public API — Getting Started](https://docs.luma.com/reference/getting-started-with-your-api) / [OpenAPI spec](https://public-api.luma.com/openapi.json) / [Luma API help](https://help.luma.com/p/luma-api) — PRIMARY. Host/calendar-scoped operations only; no attendee self-registration endpoint.
- [Luma Terms of Use](https://luma.com/terms) — PRIMARY. "must not access the Service by any means other than our publicly supported interfaces" → browser automation grey.
- [Partiful Terms of Service](https://partiful.com/terms) — PRIMARY (updated Dec 2, 2025). Bans data mining/robots/scraping; bars IP-block circumvention. No public API.
- [Van Buren v. United States, No. 19-783 (S. Ct. June 3, 2021)](https://www.supremecourt.gov/opinions/20pdf/19-783_k53l.pdf) — PRIMARY. Narrow "gates-up-or-down" CFAA reading; left open whether ToS defines authorization.
- [hiQ Labs v. LinkedIn — case history](https://en.wikipedia.org/wiki/HiQ_Labs_v._LinkedIn) — 9th Cir: CFAA doesn't reach public data; hiQ lost on breach-of-contract/trespass.
- [LinkedIn's Data Scraping Battle with hiQ Ends — $500k stipulated judgment (Privacy World)](https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/) — Dec 6, 2022: $500k, breach of User Agreement + CA trespass-to-chattels. Real risk = contract/tort + bans.
- [Ticketmaster Discovery API v2](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/) / [Partner API Terms of Use](https://developer.ticketmaster.com/support/terms-of-use/partner/) — Read-only discovery; 5000 calls/day, 5 req/s; no on-behalf action; no resale/sublicense.
- [SeatGeek Platform Terms of Use](https://seatgeek.com/api-terms) — Read-only discovery via client ID; bars displaying other sellers' listings / claiming endorsement.
