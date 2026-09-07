# Entity enrichment control plane

## Status

Migration `0133` installs a provider-neutral, dormant foundation. It makes no network calls and
does not enable LinkedIn, Levels, or any other licensed enrichment adapter. The two placeholder
provider records are disabled and cannot be claimed unless credentials are configured, terms are
reviewed, and an operator approval is recorded in a later reviewed change.

## Boundary

The catalog source remains the authority for event roles and any direct profile URL it publishes.
An opaque, database-generated `entity_id` groups observations; it is never a name hash or provider
URL. Every entity is anchored by a source-scoped stable entity ID or an already direct profile URL,
and every event-role assertion points to the exact source event and refresh run. Attendee rosters
are intentionally outside this model.

Enrichment is asynchronous:

1. Source ingestion records a bounded entity and event-role fact.
2. A future approved adapter queues a job for an exact entity revision.
3. A worker claims the job with a short, renewable token lease and bounded attempt number.
4. The worker records individual typed fields with a provider record ID, evidence URL, non-name
   match basis, confidence, observation/expiry times, and no raw response payload.
5. The job completes only from evidence written by its current attempt. A separately
   authenticated reviewer may later approve or reject each observation.
6. The guarded read returns only approved, unexpired observations for the current entity revision.
   If the source supplied a direct profile URL, an enrichment `profile_url` is never eligible to
   replace it.

Contradictory observations remain as reviewable evidence. The read capability chooses the latest
approved observation per field; it does not silently merge fuzzy identities.

Provider credentials or approval can be revoked while work is queued or leased. Claim, renew,
observation-write, and completion capabilities all re-check the provider gates; revoked work is
terminalized without retaining a raw provider error. Source-fact updates are monotonic, preserve
earlier review decisions, and use transaction-scoped locks so concurrent replay returns a bounded
outcome instead of leaking a uniqueness error.

## Provider safety

Licensed-provider adapters stay disabled until all of the following exist:

- an approved API/export or other terms-compliant evidence path (never search-result scraping);
- scoped credentials in the existing secret capability, not in jobs or observations;
- documented terms/privacy/security review and operator approval;
- provider-specific rate, retention, deletion, and audit controls;
- contract tests proving direct identity crosswalks and normalized error handling.

The general `ec_app` role cannot execute the observation-review capability. The function remains
owner-only and dormant until a later migration introduces a separately authenticated reviewer
role and binds `reviewed_by` to trusted operator identity. Collection workers therefore cannot
self-approve the evidence they collect.

Levels-style organization, role, or compensation-market facts are not evidence about a named
person. A provider adapter may only claim the entity kinds declared by its reviewed provider
configuration. Names remain display data and search text—not identity or join keys.

## Deliberate non-goals

- No synchronous enrichment during catalog ingestion.
- No LinkedIn or Levels HTTP client, scraper, credential, or fallback search URL.
- No raw provider payload persistence or consumer/admin exposure in this migration.
- No catalog mutation yet. A later materializer must use the approved-only capability and preserve
  the direct-source precedence rule.
