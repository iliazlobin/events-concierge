# G3 Test Plan — RelayInbox acceptance at source signup validators

**Gate:** G3 (requirements §8, ADR-011) — pre-design verification, still open. Gates the entire OTP/magic-link login leg and FR-2.13 account linking. If relay addresses are rejected and no fallback works, the passwordless-source login path has no in-scope solution (Gmail is forbidden).

## The two things G3 must establish, per source

1. **Acceptance** — does the source's signup/login form *accept* an `alice@u.<domain>` relay address? Validators can reject: subdomains, unusual TLDs, addresses whose domain has no web presence, plus-addressing, or disposable-domain-listed domains.
2. **Deliverability + extraction** — does the OTP / magic-link email actually *arrive* at the relay inbox (working MX + inbound routing), from an allowlisted sender, and parse deterministically (`relay_receiver.py`)?

Both must pass for a source to be served by the RelayInbox. A source that accepts the address but whose mail never arrives (or arrives from a non-allowlisted sender) fails G3 just as hard as a rejected address.

## Sources to test (launch-relevant)

| Source | Login modality | Why it matters | Pass bar |
|---|---|---|---|
| **Luma** | email magic-link / OTP (passwordless) | Flagship browser-best-effort autonomous RSVP lane (ADR-006) | address accepted **and** magic-link/OTP arrives + parses |
| **Eventbrite** | email + password (OTP on some flows) | Discovery-only, but handoff completion + change-emails route here | address accepted **and** confirmation mail arrives + parses |
| **Meetup** | OAuth (primary) + email fallback | On-SLA autonomous lane is OAuth, but account creation/verification email may still gate | address accepted at account creation |

Partiful is out (SMS-only login, disabled by default) — no relay path is built for it.

## Procedure (per source)

For each source, use a **distinct per-user alias** (`luma-test@u.<domain>`, `eventbrite-test@u.<domain>`, …) so arrivals are unambiguous and mirror the one-alias-per-user production shape.

1. **Acceptance step (manual / browser).** Open the source's signup or add-email flow and enter the source's alias. Record the immediate validator behavior:
   - `ACCEPTED` — the form proceeds to "check your email".
   - `REJECTED-FORMAT` — inline "invalid email" before submit.
   - `REJECTED-POLICY` — accepted syntactically but rejected as disposable/blocked after submit.
   Screenshot the outcome.

2. **Deliverability + extraction step (automated).** Immediately run the receiver:
   ```bash
   ./relay_receiver.py --alias luma-test --source luma --since 10 --watch 20
   ```
   It polls the relay inbox, enforces the per-source sender allowlist, and deterministically extracts the OTP/magic-link. Record `OK` (arrived + parsed) or `TIMEOUT`.

3. **Fallback verification (only if step 1 or 2 fails).** Try, in order, the ADR-011 fallbacks and record which first works:
   - **Reputable dedicated domain** — retest with a plain apex-style alias on a well-warmed domain (some validators block subdomains but accept the apex).
   - **Per-user real mailbox** — a provisioned mailbox rather than a plus/subdomain alias.
   - **First-login human-handoff** — the user completes the initial login once in a handoff; we capture the resulting session cookie (FR-2.13). This always works but adds an onboarding tap.

## Recording the result

Produce a 3x3 matrix (source × {acceptance, deliverability, chosen-fallback}) and the verdict:

- **All three sources PASS acceptance + deliverability** → G3 clears; FR-2.13 account-linking proceeds as designed; close the ADR-011 G3 rider.
- **Any source REJECTS** → record which fallback cleared it; if only first-login-handoff works for a source, that source's onboarding gains a mandatory one-time handoff step — fold into FR-2.13 and note in ADR-011 (superseding note).
- **Deliverability fails** (address accepted, mail never arrives) → the problem is MX / inbound routing or sender-allowlist coverage, not the validator; fix the relay infra and the `SOURCES[...].sender_domains` allowlist in `relay_receiver.py`, then retest.

Log the matrix + screenshots in `PROJECT.md` and update the ADR-011 G3 rider status.
