# Manual discovery acceptance

Validate the running Next.js candidate against the [private discovery scope](../PROJECT.md#current-milestone-private-discovery-candidate).
Attach sanitized acceptance evidence to the release PR. Source tests and mock runs do not establish deployed acceptance.

## Prepare

- Use an approved target and disposable account in a private browser window.
- Follow [local setup](../README.md) or [private deployment access](../deploy/development.md#shared-release-and-access).
- Record API revision, frontend digest, release/auth profiles, mock/live adapters, browser and viewport.
- Check `/versionz`, `/healthz`, `/readyz` and `/v1/ui-config` against the selected environment.
  [Readiness meanings](production-operations.md#health-readiness-and-engine-degradation) distinguish liveness, dependency health and identity readiness.
- Verify required processes through the [process inventory](production-operations.md#required-process-inventory).
  Discovery does not require deferred request/notification workers.
- Do not start, rebuild, stop or reset services as preflight. Never delete volumes or trigger provider refreshes for a UI check.
- Keep DevTools Network open. Record failing requests, correlation IDs and screenshots; exclude cookies, CSRF values, credentials and capability URLs.
- Run destructive account erasure last, only with explicit authority for that disposable account.

### Current unresolved persistent-data invariant

**Legacy observation; revalidation pending.** The retained local database reported these lifecycle counts at `2026-07-23T04:10:01Z`:

| Field | Observed value |
| --- | ---: |
| `scanned_nonterminal_workflows` | 7,800 |
| `closed_nonterminal_workflows` | 7,798 |
| `invalid_lifecycle_workflow_identity_count` | 477 |
| `active_lifecycles_missing_watch` | 111 |
| `requires_attention` | `True` |

- Root cause remains unresolved; these are not current measurements.
- Keep this defect separate from new tenant failures.
- Do not call the affected retained database invariant-clean or grant overall release acceptance until revalidated and resolved.
  A UI-only pass may be recorded separately.

## Discovery checks

Use `/v1/catalog/events` requests to verify filters and pagination. Browsing must not fetch providers or queue ingestion.

**Graph event details** — [selected-event integration tests](../tests/integration/test_catalog_browse.py), [browser checks](../tests/e2e/test_next_release_profile.py).

- [ ] Overview, entity and topic graphs stay graphical on desktop and mobile. Check node selection and keyboard navigation.
- [ ] Selecting an event in any graph shows the shared map card's source, linked title, date/time, venue links, availability, price, participants, topics and description. Overview/entity selections read `/v1/catalog/events/{canonical_event_id}`; topic graphs reuse loaded events. Initial graph load and hover do not read event details.
- [ ] Host, organizer and other entity chips resolve the selected event's exact ID, role and name, then open that entity's graph. Topic chips open the topic graph. Check overview/entity/topic graphs on desktop and mobile; profile icons still open external profiles.
- [ ] Explore connections retains source assertions and entity navigation; recurring dates select their own occurrence.
- [ ] Missing/error responses show the graph summary and a retry. Quick selection changes never display another event's details. Check desktop and mobile.

Mark checks requiring unavailable catalog data **NOT EXERCISED**.

**Filters and navigation** — [filter tests](../web/tests/event-catalog-filter-controls.test.mjs), [browser history tests](../web/tests/event-browser-history.test.mjs).

- [ ] Events, Map and Calendar retain the same query and selected filters.
- [ ] Source/place/topic multi-selects stage changes until dismissal; chips remain editable and removable.
- [ ] Multiple sources are OR selections. Sources intersect with places; cities and named scopes form a place union.
- [ ] Source changes in the filter bar preserve explicit places. Incompatible selections show an honest empty state recoverable through chips or Reset.
- [ ] Unknown place text does not invent a city, coordinates or geocode.
- [ ] Date presets send bounded windows. Two custom ranges send repeated `date_range`; deleting one preserves the other.
- [ ] Reset clears date/place constraints. Reload preserves an explicitly cleared city; Back/Forward restores the selection.
- [ ] Changing filters or Calendar month does not reuse a stale cursor. Month bounds use local month start through next month start, end-exclusive.

**Search, price and history** — [catalog integration tests](../tests/integration/test_catalog_browse.py), [price tests](../tests/integration/test_catalog_price.py).

- [ ] Search matches titles and supplied organizer/host/speaker/organization/venue/source metadata before pagination.
- [ ] Available/Sold out and Soonest/Latest ordering apply before pagination.
- [ ] `25.50` sends `2550` cents. Minimum, maximum, exact and range comparisons work; invalid/reversed amounts do not commit.
- [ ] A maximum allows free or known USD prices within the bound; adding Paid excludes free events.
  Free/Price unlisted clears amount bounds; unknown or non-USD prices do not satisfy USD comparisons.
- [ ] Past-range rows and source counts use the same interval. Retained past observations may survive crawl rolloff; future entries require the current projection.
- [ ] Empty history means no eligible retained events, not proof the provider had none. Latest-known event state is not an as-of archive.

**Event details and map** — [event semantics](../design/catalog-event-semantics.md), [entity boundaries](../design/entity-catalog-and-research.md).

- [ ] One list card expands at a time. Details retain source, price, location and past/ongoing labels without inventing missing facts.
- [ ] The card's source shortcut opens source history and clears query, custom dates and places.
- [ ] Event titles open the retained provider URL in a new tab across Events, Map previews/details, Calendar and Graph (including loading/error summaries). Separate registration actions are absent. Titles without a safe URL remain plain text. Pointer and keyboard title activation never toggle details or select a map marker; disclosure and map-focus controls still work.
- [ ] Calendar opens a draft; venue opens Maps. These actions do not toggle the card or claim registration/synchronization.
- [ ] Map markers and preview cards select the same event. The preview rail follows map bounds; place changes fit event points or selected-scope fallback bounds.
- [ ] External entity links require verified direct HTTPS profiles/sites; no generated LinkedIn search links or named attendee rosters.
- [ ] For an already populated, reviewed Meetup source: public event links and source labels work; zero-event success remains distinct from transport failure.
  [Meetup ingestion](meetup-ingestion-runbook.md) owns refresh setup and provider limits.

## Accounts and usability

**Identity and product scope** — [sign-in browser tests](../tests/e2e/test_next_sign_in.py), [release-profile browser tests](../tests/e2e/test_next_release_profile.py).

- [ ] UI configuration resolves before onboarding/navigation; failure offers retry without exposing deferred features.
- [ ] Discovery omits chat, managed RSVP/handoffs, notifications, Calendar sync, purchases and API-key controls, including direct links.
- [ ] Local-demo onboarding and deployment sign-in follow their configured paths. Deployment sessions do not accept local tenant-header impersonation.
- [ ] Expired deployment sessions lead to sign-in without an automatic OAuth loop.
- [ ] Successful logout clears the browser session reference. Failed server logout remains visible and does not claim revocation.
  Local-demo sign-out removes browser access only; it does not erase server data.
- [ ] Google deletion remains disabled with its stated reauthentication limitation; signing in again does not enable it.
  Google configuration and deployed identity acceptance remain [release gates](production-operations.md#first-release-acceptance).

**Profile and saved filters** — [profile tests](../web/tests/account-profile-form.test.mjs), [saved-filter UI tests](../web/tests/event-saved-filters.test.mjs), [tenant isolation tests](../tests/integration/test_saved_catalog_filters.py).

- [ ] Profile changes persist after reload; validation/save failures remain visible. Email is not silently rebound to another identity.
- [ ] Save, apply, rename and delete a filter; applied selections survive navigation and reload. Rename preserves its selection.
- [ ] A second authorized disposable account cannot see or modify the first account's profile or saved filters.
  UI observations alone do not prove database tenant isolation.

**Usability and recovery**

- [ ] At `390 × 844`, Events/Map/Calendar and settings remain usable without overlapping controls or horizontal page overflow.
- [ ] Keyboard focus is visible; filters, details and settings actions work without a mouse. Dismissible overlays close and return focus sensibly.
- [ ] Reduced motion remains usable. Loading, empty and error states stay distinct; retry preserves intended filters.
- [ ] Reload preserves supported session/filter state. Record browser storage restrictions instead of claiming persistence passed.

## Account erasure — destructive, last

Use only an explicitly approved disposable account. Verify its exact tenant and contact identity before submission.
The account-erasure worker and relevant cleanup adapters must be available. For deployment sessions, follow the
[identity and recent-auth contract](production-operations.md#built-in-oidc-bff-activation); Google destructive reauthentication is unsupported.

1. Save representative profile/saved-filter data. Open Settings → Account → Erase this account.
2. Check lowercase and trailing-space variants: neither enables erasure. Cancel; account remains usable.
3. Enter exactly `DELETE MY ACCOUNT`; submit once. Capture the accepted `request_id`.
4. Verify the underway receipt, cleared browser session/caches and no authenticated completion polling.
   An accepted request does not prove cleanup completion. A lost-response retry must reuse the same request ID.
5. Have an authorized operator verify the tenant-specific command tombstone and cleanup evidence; global worker counters are insufficient.

Completion requires:

- Matching request ID, `status=completed`, completed stage timestamps, no active lease and no unresolved failure stage.
- External effects drained; workflows, Calendar targets, browser sessions, vault and object storage cleanup verified.
- Tenant-owned rows removed; retained audit rows have consent references removed and PII shredded.
- Permanent pseudonymous, payload-free erasure tombstone retained; old tenant access rejected.
- In-progress fencing remains until cleanup succeeds. An unavailable adapter or failure is pending/failed evidence, never permission for a new deletion ID.

Exact database and workflow assertions live in [erasure integration tests](../tests/integration/test_account_erasure.py)
and [Temporal erasure tests](../tests/integration/test_account_erasure_temporal.py).
Mock success does not prove real provider deletion, Redis-wide revocation, object-store cleanup, history/archive removal,
backup expiry, retention policy or production deadlines.

## Record acceptance

- Record **PASS**, **FAIL** or **PARTIAL** against the exact running candidate; list failed, skipped and unexercised checks.
- Include sanitized evidence and the first reproducible failure. Keep implementation, CI, local acceptance and deployed acceptance separate.
- Preserve the retained-data defect above until resolved; apply all [release gates](production-operations.md#first-release-acceptance).
- Full-profile request/lifecycle behavior remains deferred. Its contracts live in [request-start](../tests/integration/test_request_start.py)
  and [workflow tests](../tests/integration/test_workflow.py): durable acceptance, duplicate rejection, recovery and verified outcomes.
  Do not enable provider writes or simulate outages as part of discovery acceptance.
