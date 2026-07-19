#!/usr/bin/env python3
"""
G3 spike -- RelayInbox receiver + deterministic OTP/magic-link extractor.

Serves the G3 gate (requirements §8, ADR-011): do Luma / Eventbrite / Meetup
signup validators ACCEPT per-user relay addresses (alice@u.<domain>), AND does
the login code actually ARRIVE and parse out of the relay inbox?

This script answers the second half (arrival + extraction) and doubles as a
working prototype of the FR-5.7 / FR-5.8 EmailIngestionPort:
  - polls the relay inbox for mail to a given per-user alias,
  - enforces a per-SOURCE sender-domain allowlist (FR-10.6 -- untrusted email is
    an indirect-injection vector),
  - deterministically extracts the OTP code / magic-link WITHOUT sending raw HTML
    to any LLM (FR-5.7),
  - treats the code as a short-TTL secret: it is printed for the operator's
    verification but NOT logged to disk (FR-5.8).

Two backends, pick by env:
  RELAY_BACKEND=imap   -> poll a real mailbox over IMAP (simplest for a test domain)
  RELAY_BACKEND=s3     -> poll an S3 prefix that SES inbound writes to (production shape)

Usage:
  export RELAY_BACKEND=imap
  export IMAP_HOST=imap.<provider> IMAP_USER=... IMAP_PASS=...
  ./relay_receiver.py --alias luma-test --source luma --since 15
      # poll for mail to <alias>@u.<domain> from luma's allowlisted senders in the last 15 min

  export RELAY_BACKEND=s3
  export RELAY_S3_BUCKET=... RELAY_S3_PREFIX=inbound/
  ./relay_receiver.py --alias eventbrite-test --source eventbrite --since 30

No third-party deps for imap (stdlib imaplib/email). s3 backend needs boto3.
"""
import argparse
import email
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header

# Per-source config: allowlisted sender domains + deterministic code/link patterns.
# This mirrors the FR-5.7 EmailIngestionPort's per-source extractor registry.
SOURCES = {
    "luma": {
        "sender_domains": ["lu.ma", "luma-mail.com", "e.lu.ma"],
        "otp_patterns": [r"\b(\d{6})\b"],
        "link_patterns": [r"https://lu\.ma/[^\s\"'<>]+"],
    },
    "eventbrite": {
        "sender_domains": ["eventbrite.com", "order.eventbrite.com", "noreply.eventbrite.com"],
        "otp_patterns": [r"\b(\d{6})\b"],
        "link_patterns": [r"https://www\.eventbrite\.com/[^\s\"'<>]+"],
    },
    "meetup": {
        "sender_domains": ["meetup.com", "e.meetup.com", "info.meetup.com"],
        "otp_patterns": [r"\b(\d{4,8})\b"],
        "link_patterns": [r"https://(?:www\.)?meetup\.com/[^\s\"'<>]+"],
    },
}


def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    return "".join((p.decode(enc or "utf-8", "ignore") if isinstance(p, bytes) else p) for p, enc in parts)


def _plaintext(msg):
    """Extract text/plain (preferred) or a tag-stripped text/html body. No LLM, ever."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                return re.sub(r"<[^>]+>", " ", html)
    payload = msg.get_payload(decode=True)
    if payload:
        body = payload.decode(msg.get_content_charset() or "utf-8", "ignore")
        return re.sub(r"<[^>]+>", " ", body) if msg.get_content_type() == "text/html" else body
    return ""


def sender_allowed(from_addr, source_cfg):
    m = re.search(r"@([\w.-]+)", from_addr or "")
    dom = (m.group(1) if m else "").lower()
    return any(dom == d or dom.endswith("." + d) for d in source_cfg["sender_domains"]), dom


def extract(body, source_cfg):
    """Deterministic OTP + magic-link extraction. Returns (otp_or_None, link_or_None)."""
    otp = None
    for pat in source_cfg["otp_patterns"]:
        m = re.search(pat, body)
        if m:
            otp = m.group(1)
            break
    link = None
    for pat in source_cfg["link_patterns"]:
        m = re.search(pat, body)
        if m:
            link = m.group(0)
            break
    return otp, link


def poll_imap(alias, source_cfg, since_min):
    import imaplib
    host, user, pw = os.environ["IMAP_HOST"], os.environ["IMAP_USER"], os.environ["IMAP_PASS"]
    to_addr = f"{alias}@{os.environ.get('RELAY_DOMAIN', 'u.example.com')}"
    M = imaplib.IMAP4_SSL(host)
    M.login(user, pw)
    M.select("INBOX")
    since = (datetime.now(timezone.utc) - timedelta(minutes=since_min)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(TO "{to_addr}" SINCE {since})')
    out = []
    for num in (data[0].split() if data and data[0] else []):
        typ, raw = M.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(raw[0][1])
        out.append(msg)
    M.logout()
    return out, to_addr


def poll_s3(alias, source_cfg, since_min):
    import boto3
    s3 = boto3.client("s3")
    bucket, prefix = os.environ["RELAY_S3_BUCKET"], os.environ.get("RELAY_S3_PREFIX", "inbound/")
    to_addr = f"{alias}@{os.environ.get('RELAY_DOMAIN', 'u.example.com')}"
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_min)
    out = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            msg = email.message_from_bytes(raw)
            if to_addr.lower() in (_decode(msg.get("To", "")) or "").lower():
                out.append(msg)
    return out, to_addr


def main():
    ap = argparse.ArgumentParser(description="G3 RelayInbox receiver + extractor")
    ap.add_argument("--alias", required=True, help="per-user alias local-part, e.g. luma-test")
    ap.add_argument("--source", required=True, choices=list(SOURCES), help="which source's email you expect")
    ap.add_argument("--since", type=int, default=15, help="look back N minutes")
    ap.add_argument("--watch", type=int, default=0, help="poll every N seconds until a code arrives (0 = one-shot)")
    args = ap.parse_args()

    cfg = SOURCES[args.source]
    backend = os.environ.get("RELAY_BACKEND", "imap")
    poll = {"imap": poll_imap, "s3": poll_s3}[backend]

    def once():
        msgs, to_addr = poll(args.alias, cfg, args.since)
        print(f"[{backend}] {len(msgs)} message(s) to {to_addr} in the last {args.since} min")
        found = False
        for msg in msgs:
            frm = _decode(msg.get("From", ""))
            subj = _decode(msg.get("Subject", ""))
            allowed, dom = sender_allowed(frm, cfg)
            print(f"  - from={frm} subject={subj!r} sender_domain={dom} allowlisted={allowed}")
            if not allowed:
                print("    DROPPED: sender not on the per-source allowlist (FR-10.6).")
                continue
            body = _plaintext(msg)
            otp, link = extract(body, cfg)
            # FR-5.8: the code is a short-TTL secret -- print for operator verification, never persist.
            print(f"    ACCEPTED: otp={'<'+str(len(otp))+'-digit code present>' if otp else None} "
                  f"magic_link={'<link present>' if link else None}")
            if otp or link:
                found = True
                # Uncomment to reveal during an interactive run only:
                # print(f"      code={otp} link={link}")
        return found

    if args.watch:
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            if once():
                print("EXTRACTION OK -- a code/link arrived and parsed deterministically.")
                return
            time.sleep(args.watch)
        print("TIMEOUT -- no allowlisted code arrived. Check MX/inbound routing and whether the source accepted the alias.")
    else:
        ok = once()
        print("\nG3 arrival+extraction:", "OK" if ok else "NONE FOUND (see notes in RUNBOOK.md)")


if __name__ == "__main__":
    main()
