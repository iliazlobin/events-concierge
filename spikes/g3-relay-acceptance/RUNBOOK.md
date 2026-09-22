# G3 Runbook — RelayInbox acceptance & delivery

**Gate:** G3 (requirements §8, ADR-011). See `test-plan.md` for the per-source procedure and pass bars; this runbook is setup + how to run the receiver.

## What you must provide

1. **A relay domain** you control, e.g. `u.<yourdomain>`, with **working inbound email**. Two ways to stand it up:
   - **Quick (IMAP):** any mailbox provider that accepts catch-all or per-alias addresses on the domain (so `luma-test@u.<domain>`, `eventbrite-test@u.<domain>` all land in one inbox). Gives you an IMAP host/user/pass.
   - **Production-shape (SES→S3):** an AWS SES inbound receipt rule set for `u.<domain>` writing raw MIME to an S3 prefix — the exact `d16` architecture. Gives you a bucket + prefix.
2. **MX records** for `u.<domain>` pointing at the chosen receiver (the provider's MX, or SES inbound `inbound-smtp.<region>.amazonaws.com`), plus SPF/DKIM/DMARC so source senders do not spam-drop.
3. Nothing from Meetup/Luma/Eventbrite beyond ordinary signup — the test is precisely whether their signup accepts the address.

## Setup — IMAP backend (fastest)

```bash
cd spikes/g3-relay-acceptance
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # stdlib only for imap; boto3 only if you use s3
export RELAY_BACKEND=imap
export RELAY_DOMAIN=u.<yourdomain>
export IMAP_HOST=imap.<provider> IMAP_USER=<mailbox user> IMAP_PASS=<app password>
```

## Setup — S3 backend (production shape)

```bash
export RELAY_BACKEND=s3
export RELAY_DOMAIN=u.<yourdomain>
export RELAY_S3_BUCKET=<bucket> RELAY_S3_PREFIX=inbound/
# AWS creds via the usual chain (env / profile / role) with s3:GetObject + s3:ListBucket
pip install -r requirements.txt          # installs boto3
```

## Run

Per source, do the manual acceptance step in a browser (test-plan.md step 1), then:

```bash
./relay_receiver.py --alias luma-test       --source luma       --since 10 --watch 20
./relay_receiver.py --alias eventbrite-test --source eventbrite --since 10 --watch 20
./relay_receiver.py --alias meetup-test     --source meetup     --since 10 --watch 20
```

`--watch 20` polls every 20s for up to 30 min until an allowlisted code/link arrives. One-shot (omit `--watch`) checks once and exits.

## What the receiver enforces (and why it is also a prototype)

The receiver is a faithful prototype of the **FR-5.7/FR-5.8 EmailIngestionPort**, so a passing G3 run doubles as validated ingestion code:
- **Sender-domain allowlist per source** (`SOURCES` in `relay_receiver.py`) — an email from an off-allowlist sender is DROPPED (FR-10.6, untrusted email is an injection vector). If a real code arrives from a sender domain not in the list, add it and note it — that is coverage data, not a failure.
- **Deterministic extraction** — regex OTP + magic-link patterns, no raw HTML ever handed to an LLM (FR-5.7).
- **Short-TTL secret handling** — the code is reported as "present" but not written to disk (FR-5.8); reveal it interactively only by uncommenting the marked line during a live run.

## Interpreting into the design

- **All sources accept + deliver + parse** → G3 clears. Close the ADR-011 G3 rider; FR-2.13 account-linking proceeds; the `SOURCES` allowlist you validated seeds the production EmailIngestionPort config.
- **A source rejects the alias** → walk the `test-plan.md` fallback ladder (dedicated domain → per-user mailbox → first-login handoff) and record which clears it. If only first-login-handoff works for a source, its onboarding gains a one-time mandatory handoff — fold into FR-2.13 and write a superseding note on ADR-011.
- **Accepts but no delivery** → MX/inbound-routing or allowlist gap, not a validator problem; fix infra + `sender_domains`, retest.

Record the source × {acceptance, deliverability, fallback} matrix in the owning GitHub issue or PR and update the ADR-011 G3 rider.
