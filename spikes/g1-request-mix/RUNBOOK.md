# G1 — deployed request-mix measurement

G1 estimates the share of realistic requests whose highest-ranked registerable candidate begins in
the autonomous-SLA, browser-best-effort, or human-handoff lane. It also records requests with no
registerable candidate. The harness calls only the read-only preview endpoint (`POST /v1/feed`); it
does not create a durable request, workflow, RSVP, calendar entry, or notification.

## Prepare the evidence input

1. Copy `corpus.example.json` outside version control and replace the example with a consented,
   representative natural-language corpus. Use opaque sequential labels such as `scenario-0001`;
   the harness rejects descriptive labels that could leak user or prompt information. Do not include
   names, email addresses, credentials, private event links, or copied customer messages.
2. Freeze the source registry, policy versions, catalog refresh, measurement account membership,
   deployment version, geography, and measurement window. Record those facts in the release ticket.
3. Provision a representative measurement user. Because Meetup membership changes routing, a
   single unrepresentative account cannot establish the whole population mix; stratify runs by
   documented persona/account cohort and combine them outside this harness with approved weights.

## Run

Authenticate with either the exact authorization header or an opaque BFF cookie through the
environment, never both. The harness never prints or writes either value and refuses to send one
to plaintext or ambiguous origins. Loopback HTTP is available only for a non-field rehearsal with
the explicit `--allow-http-local` flag.

```bash
export G1_BASE_URL=https://staging.example.com
export G1_TRUSTED_ORIGIN=https://staging.example.com  # independently verify before exporting
export G1_COOKIE='__Host-ec_session=...'
uv run python -m events_concierge.quality.request_mix \
  --corpus /secure/path/g1-corpus.json \
  --deployment-label staging-release-candidate \
  --output /secure/path/g1-report.json
```

For a bearer-auth deployment, set `G1_AUTHORIZATION='Bearer ...'` instead of `G1_COOKIE`. Run
against staging first. A non-200 response or malformed contract stops the whole run; the body is not
retained because it may contain event details.

## Interpret without overclaiming

The harness accepts at most 500 prompts and spaces calls by 0.5 seconds by default because feed
preview can consume calendar and ranking-provider capacity. The report contains the exact corpus
digest, aggregate counts/percentages, and per-scenario lane,
candidate count, and latency. It intentionally excludes prompts, event data, identifiers, URLs,
credentials, and raw responses. A prior report is never overwritten.

This is a server-returned lane-hint estimate at the deployed discovery/ranking/policy boundary.
The current preview does not make the execution workflow's fresh tenant-and-event Meetup
membership read, so this report alone cannot close G1; membership-aware cohort evidence must be
collected through the approved real execution path once G2/provider activation is available. It is not a
provider success-rate, completion-latency, SLA, legal approval, load test, or proof that the corpus
is representative. Owner review must approve the corpus/cohort weights, then use the measured mix
to resize browser concurrency, Ticketmaster re-poll reserve, and handoff staffing before G1 closes.
