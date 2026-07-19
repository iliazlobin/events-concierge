# Meetup RSVP Prerequisite Chain: The Join Step Has No API Surface

> Closes the post-wave-1 gap of assuming Meetup's `createEventRsvp` is a standalone on-SLA autonomous register path; it is not, because RSVP is gated on prior group membership and the join step is not automatable via the official API — forcing a downgrade of the guaranteed coverage claim.

## Verified findings

### Load-bearing

- **[confirmed] RSVP is membership-gated; `createEventRsvp` for a non-member is not a safe auto-success and must be treated as requiring a prior join step.** Meetup's help center states verbatim: "Only members of your group can attend an event that you create for that group." The legacy REST API modeled these as two distinct operations — `CreateProfile` ("join a group by creating a profile") and separately `CreateRsvp` — confirming RSVP was never the join primitive. On the current GraphQL API, the runtime behavior of `createEventRsvp` against a group the token-owner has not joined is **not documented**: it is unverified whether it silently auto-joins (legacy open-group behavior), returns a hard membership error, or lands the user in a pending/approval state. Resolve by a build-time spike, never by assumption.

- **[confirmed] There is NO `joinGroup`-class mutation in the current Meetup GraphQL schema — self-service group membership is not automatable via the official API.** A schema introspection snapshot (meetupr CRAN vignette) enumerates the full mutation set — `addGroupToNetwork, announceEvent, closeEventRsvps, createEvent, createGroupDraft, createGroupEventPhoto, createVenue, deleteEvent, deleteGroupDraft, editEvent, openEventRsvps, publishEventDraft, publishGroupDraft, updateGroup, updateGroupDraft, updateGroupMembershipRole` — with no `joinGroup`, no `createProfile` equivalent, and no self-service membership-create mutation. `updateGroupMembershipRole` is organizer-only (grant/change a role for an existing member), not a self-join. The legacy REST `CreateProfile` join primitive was not carried forward into GraphQL; the `Membership` type is query-only (read-only). The Feb 2025 GraphQL refresh added full introspection support but no join mutation. Snapshots can lag the live schema — confirm via live introspection at build time — but current evidence is that the join step the RSVP chain depends on has no API surface at all.

- **[confirmed] Real Meetup groups impose approval queues, screening questions, and paid dues — none auto-satisfiable via API; each is a hard human/payment gate that breaks autonomous join.** Three independent, organizer-configurable gates verify against primary help-center content:
  - **Member approval** — private groups (and public groups with "New member approval" enabled) hold every request in Pending until an organizer manually accepts/denies. Asynchronous human queue.
  - **Screening/profile questions** — organizers can require up to five free-form profile questions answered at join/RSVP time, plus optionally a profile photo. Free-form text reviewed by leadership; not API-representable.
  - **Member dues** — Standard/Pro organizers can require paid dues (minimum $1.50; Meetup fee 7.5% + $0.50; optional 14–180 day free trial) to join and/or RSVP. Dues groups render a "Request to join" button; payment is required to gain/retain membership.

  Consequence: even if a join mutation existed, only **OPEN, no-question, no-dues, instant-join public groups** would be autonomously joinable.

- **[confirmed] The API license is revocable at Meetup's sole discretion, Pro-gated, non-sublicensable and non-transferable — a single-point-of-failure legal risk for the sole on-SLA register path, with no contractual multi-tenant safe harbor.** Meetup grants "a limited, non-exclusive, non-transferable, non-sublicensable, revocable license to use the Meetup API," which "terminates automatically if you violate any provision... if Meetup provides written notice of termination, or if Meetup discontinues your access." Meetup "reserves the right to deny or revoke licenses... under our sole discretion" and "may modify, suspend, or terminate your access... if they determine in their sole discretion that your use... undermines their commercial interests or for any other reason." Creating an OAuth consumer requires an active Meetup **Pro** subscription, and "having a Pro subscription does not guarantee approval."
  - **[adjusted] The more direct legal exposure is a commercial-use clause, not just the sublicense ambiguity.** The terms additionally prohibit using the API "for any commercial purpose without the express written consent of Meetup" — a sharper risk for a paid multi-tenant concierge than the non-sublicensable/non-transferable language. There is no explicit multi-tenant / on-behalf-of safe harbor; the capability is revocable at sole discretion.
  - **[adjusted] Only the OAuth client *creator* needs Meetup Pro; end-users who authorize via OAuth do NOT need their own Pro subscription.** This makes an on-behalf-of architecture technically feasible, but does not resolve the contractual ambiguity.

- **[adjusted] Rate limit is 500 points/60s; per-consumer+member sharding is forum-sourced (unconfirmed), and every end user must complete their own interactive OAuth authorization.** The GraphQL guide confirms "500 points in your queries every 60 seconds," returning code `RATE_LIMITED` with `consumedPoints` and a `resetAt` timestamp. A developer-forum statement claims the limit is "tallied by the consumer + authenticated member" (i.e. it shards per-user rather than one global pool) — favorable for multi-tenant scale, but **[unverifiable]** against the official guide; verify by spike. Critically, the auth docs show the server-to-server JWT flow's "member authorization is limited to the owner of the OAuth Client." Acting for arbitrary end-users therefore requires the interactive Authorization Code flow where each tenant user grants their own token (refresh tokens single-use). You cannot mint tokens for arbitrary members from one Pro consumer without their individual OAuth consent. A separate forum thread confirms: passing an explicit `member_id` implies org/host acting on behalf and a non-host gets 401 — omit `member_id` to RSVP as the authenticated member.

### Non-load-bearing

- **[researcher: low confidence] No published Meetup-wide statistic exists for the open vs approval/questionnaire/dues split.** Searches of the help center, blog, and developer forums surface no breakdown of what fraction of active public groups are open-instant-join vs gated. Qualitatively, casual/large public groups skew open-join while professional, niche, safety-sensitive, and monetized groups increasingly use approval, screening, and dues (dues being a promoted Pro monetization feature). Defensible on-SLA claim: autonomous RSVP for groups where the user is **already** a member, plus (pending a positive spike) open no-dues instant-join groups; approval/questionnaire/dues groups are inherently human-handoff.

## Design implications

### Coverage tiering (downgrade the launch claim)

| Tier | Group condition | Lane | SLA |
|---|---|---|---|
| Guaranteed | User is ALREADY a member | Autonomous `createEventRsvp` | On-SLA |
| Conditional upside | Open, no-question, no-dues, instant-join public group | Autonomous — only if build-time spike confirms `createEventRsvp` auto-joins or a join path exists | On-SLA only after spike passes |
| Human-handoff | Approval-gated, screening-question, or dues-required group | Surface deep link for user to join manually | OFF-SLA (same lane as Luma/Eventbrite) |

Downgrade the realistic Meetup on-SLA launch claim from "all Meetup events" to "events in groups the user is already a member of." Do not market "autonomous RSVP on your behalf" across all Meetup until the spike closes the unknowns.

### Workflow / lifecycle

- Model membership as an explicit precondition state in the `(user, event)` workflow. Before `createEventRsvp`, the activity must query group membership and branch. Do NOT assume RSVP auto-joins. Lifecycle path: `discovered -> needs_membership -> (auto_joinable | human_handoff) -> registered` — not a single `register()` call.
- The Meetup adapter's `register()` must hard-gate on pre-existing membership if the spike confirms no join mutation: attempt RSVP only when membership is already true; otherwise emit a human-handoff task with a group-join deep link, kept entirely off the reliability SLA.

### Build-time API spike (blocking, before shipping the on-SLA promise)

Run against a live Pro OAuth consumer + 2–3 fresh test member accounts (mirror the browser-success-rate spike). Assert:
1. Live GraphQL introspection for ANY join-group / create-profile mutation.
2. `createEventRsvp` behavior for a token-owner who is NOT a member of an OPEN group (auto-join? error? pending?).
3. Same against an approval-gated group and a dues group.
4. Confirm the 500pt limit is per consumer+member.

Do not ship the on-SLA register promise until #1 and #2 are answered.

### Auth / credential architecture

- Each tenant user completes their own interactive Authorization Code consent for Meetup (per-user refresh tokens, single-use rotation). Do NOT act for users via the owner-only JWT flow. Do NOT pass explicit `member_id` (triggers on-behalf-of semantics → 401 for non-hosts); RSVP as the authenticated member.
- Store per-user Meetup tokens in the KMS-envelope vault with per-tenant isolation.

### Resilience / legal

- Plan graceful degradation for consumer revocation. The single Pro OAuth consumer is a sole-discretion, non-sublicensable single point of failure for the ONLY on-SLA autonomous register path. The system must (a) detect `RATE_LIMITED`, consumer-revocation, and auth errors distinctly; (b) fail Meetup registrations over to the browser best-effort + human-handoff lane automatically; (c) never let a Meetup consumer outage cascade into a hard product SLA breach.
- Obtain legal review of the commercial-purpose clause ("no commercial use without express written consent") and the non-sublicensable clause before marketing autonomous RSVP at scale.

### Throughput

- If the spike confirms per-consumer+member sharding, budget 500pts/60s per authenticated user token. Use the 3-tier model routing to keep GraphQL query cost low per RSVP so a single user's workflow stays well under the window.

## Sources

- [Meetup GraphQL API Guide](https://www.meetup.com/graphql/guide/) — Primary. 500 points/60s; `RATE_LIMITED` with `consumedPoints`/`resetAt`. No `createEventRsvp`/`joinGroup` example or documented membership requirement. Notes Feb 2025 `rsvp`→`rsvps` rename.
- [Meetup GraphQL Authentication](https://www.meetup.com/graphql/authentication/) — Primary. JWT flow member authorization limited to OAuth Client owner; interactive Authorization Code flow required for other members; refresh tokens single-use.
- [Meetup schema introspection (meetupr CRAN vignette)](https://cran.r-project.org/web/packages/meetupr/vignettes/introspection.html) — Introspection snapshot: 16 mutations, no `joinGroup`/`createProfile`; only organizer-only `updateGroupMembershipRole`. May lag live schema.
- [Legacy Meetup REST API docs (readthedocs)](https://meetup-api.readthedocs.io/en/latest/meetup_api.html) — Legacy modeled join (`CreateProfile`) and RSVP (`CreateRsvp`) as distinct ops; RSVP recorded for the authenticated member.
- [Meetup Help — Enable a waitlist](https://help.meetup.com/hc/en-us/articles/360003883411-Enable-a-waitlist-for-a-Meetup-event) — "Only members of your group can attend an event." Attendance is membership-gated. Direct fetch 403; captured via indexed snippet.
- [Meetup Help — Public vs Private Groups](https://help.meetup.com/hc/en-us/articles/360002921751-Key-differences-between-Public-Groups-and-Private-Groups) — Member-approval gate: request-to-join + wait for organizer approval; screening profile questions.
- [Meetup Help — Controlling who joins](https://help.meetup.com/hc/en-us/articles/360002878091-How-do-I-control-who-joins-my-Meetup-group-) — "New member approval" holds requests Pending until organizer accepts/denies.
- [Meetup Help — Profile and event questions](https://help.meetup.com/hc/en-us/articles/360022471332-Profile-and-event-questions) — Up to five free-form profile questions and optional required photo at join/RSVP.
- [Meetup Help — Setting member dues](https://help.meetup.com/hc/en-us/articles/360002877671--Membership-dues-How-do-I-set-member-dues-for-the-groups-I-organize) — Dues min $1.50; 7.5% + $0.50 fee; 14–180 day free trial; gates join and/or RSVP.
- [Meetup Help — Joining a group with member dues](https://help.meetup.com/hc/en-us/articles/9218722540045-Joining-a-group-with-member-dues) — Dues groups show "Request to join"; payment required to gain/retain membership.
- [Meetup API License Terms](https://help.meetup.com/hc/en-us/articles/360028705532-Meetup-API-license-terms) — Limited, non-exclusive, non-transferable, non-sublicensable, revocable; auto-terminates on violation/notice/discontinuation; sole-discretion revocation; no commercial use without express written consent.
- [Meetup Help — How can I get access to Meetup's API](https://help.meetup.com/hc/en-us/articles/41453576628749-How-can-I-get-access-to-Meetup-s-API) — Active Pro subscription required to create OAuth consumers; approval not guaranteed.
- [Meetup Help — Using Meetup's API](https://help.meetup.com/hc/en-us/articles/360028901812-Using-Meetup-s-API) — Corroborates access/usage terms.
- [Google Groups meetup-api — RSVP 401 / on-behalf-of](https://groups.google.com/g/meetup-api/c/h8p2Krw7PSA) — Explicit `member_id` implies org/host acting on behalf; non-host gets 401. Omit `member_id` to RSVP as authenticated member.
- [Google Groups meetup-api — rate limit tallying](https://groups.google.com/g/meetup-api/c/R5FCSjjJaEM) — Forum claim: limit tallied per consumer + authenticated member (shardable). Not confirmed in official guide — verify by spike.
- [Meetup Blog — Monetizing Meetup: Setting Member Dues](https://www.meetup.com/blog/recording-monetizing-meetup-setting-member-dues/) — Dues promoted as Pro monetization; qualitative signal that professional/monetized groups increasingly gate join.
