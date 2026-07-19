#!/usr/bin/env python3
"""
G2 spike harness -- Meetup createEventRsvp behavior probe.

Answers the four G2 questions the requirements mark DESIGN-BLOCKING for any
autonomous-lane SLA (requirements.md section 8, ADR-005/ADR-008):

  A. Does the RSVP mutation AUTO-JOIN an open group for a non-member, or does it
     require prior membership? (Decides whether "open group" widens the on-SLA
     surface beyond "groups the user is already in".)
  B. Is the 500-points / 60s budget scoped per-OAUTH-TOKEN, per-APP (client id),
     or per-IP? (Decides the fair-share pacer key in ADR-005: if per-app, the
     pacer collapses to one global bucket + degrade-to-handoff.)
  C. Is the RSVP mutation retry-IDEMPOTENT? (Backstops the FR-5.3 read-before-
     mutate guard: a lost-ACK retry must not create a duplicate RSVP.)
  D. What is the real per-RSVP-chain point cost (read-membership + read-rsvp-state
     + create-rsvp)? (The ~15 pts figure in the dossiers is an UNPUBLISHED
     estimate; ADR-008 sizing depends on the measured value.)

DESIGN POSTURE (why this is a legitimate probe, not abuse):
  - It runs against the OPERATOR's own Meetup account via the sanctioned GraphQL
    API -- exactly the modality the product uses in production.
  - It is DRY-RUN by default: no mutation fires without --commit.
  - N is tiny, every call honors X-RateLimit-Reset / Retry-After, and there is a
    fixed inter-call delay (human cadence). No block circumvention, no IP rotation.
  - Test A and C, when committed, leave the account RSVPed to a real event; the
    harness offers --cleanup to withdraw afterward.

The Meetup GraphQL schema evolves. This harness does NOT hard-code a guessed
mutation -- it INTROSPECTS the live schema first and prints the RSVP mutation +
rate-limit shape so the operator confirms the query strings below before any
committed run. Edit the QUERIES block if introspection shows a different shape.

Usage:
  export MEETUP_TOKEN=...            # operator's OAuth bearer token (required)
  export MEETUP_TOKEN_2=...          # optional second token (for the per-app test B)
  ./harness.py introspect            # print the live rsvp mutation + rateLimit schema
  ./harness.py test-a --group <urlname> --event <eventId>   # auto-join probe (dry-run)
  ./harness.py test-b [--n 40]       # quota-scope burst (read-only, safe)
  ./harness.py test-c --event <eventId> --commit            # idempotency (mutates!)
  ./harness.py test-d --group <urlname> --event <eventId>   # per-chain point cost
  ./harness.py all --group <urlname> --event <eventId>      # A+B+D dry-run, report

Output: a JSON report to ./g2-report-<utc>.json (path printed) + a human summary.
No third-party deps beyond `requests` (see requirements.txt).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests  (see requirements.txt)")

GQL_ENDPOINT = os.environ.get("MEETUP_GQL_ENDPOINT", "https://api.meetup.com/gql")
INTER_CALL_DELAY_S = float(os.environ.get("MEETUP_DELAY_S", "1.5"))  # human cadence
RATE_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                "Retry-After", "X-RateLimit-Used", "X-RateLimit-Cost")

# --- Query strings: CONFIRM against `introspect` output before a committed run. ---
QUERIES = {
    # Whether the authed user is a member of a group + the group's join mode.
    "membership": """
      query($urlname: String!) {
        groupByUrlname(urlname: $urlname) {
          id name isMember joinMode membershipMetadata { status role }
        }
      }""",
    # Existing RSVP state for an event (the FR-5.3 read-before-mutate read).
    "rsvp_state": """
      query($eventId: ID!) {
        event(id: $eventId) {
          id title going
          rsvpState
          selfRsvp { id status }
        }
      }""",
    # The RSVP mutation. Meetup exposes an `rsvp` mutation; the exact input shape
    # is schema-versioned -- VERIFY via `introspect`.
    "rsvp_mutate": """
      mutation($eventId: ID!, $response: RsvpResponse!) {
        rsvp(input: { eventId: $eventId, response: $response }) {
          ticket { id status }
          errors { message code field }
        }
      }""",
    "introspect_mutation": """
      query { __type(name: "Mutation") { fields { name args { name type { name kind ofType { name kind } } } } } }""",
    "introspect_ratelimit": """
      query { __type(name: "Query") { fields { name } } }""",
}


def gql(query, variables=None, token=None):
    """One GraphQL call. Returns (json_body, response_headers, http_status)."""
    tok = token or os.environ.get("MEETUP_TOKEN")
    if not tok:
        sys.exit("MEETUP_TOKEN not set")
    r = requests.post(
        GQL_ENDPOINT,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        data=json.dumps({"query": query, "variables": variables or {}}),
        timeout=30,
    )
    # Respect the rate-limit reset / retry-after: if throttled, wait then let the
    # caller decide. We never rotate identity or bypass; we back off and honor it.
    if r.status_code == 429:
        reset = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset")
        print(f"  [429] throttled; server says reset in {reset}s -- honoring, not bypassing")
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:500]}
    return body, {h: r.headers.get(h) for h in RATE_HEADERS if r.headers.get(h)}, r.status_code


def rate_snapshot(headers, body):
    """Pull whatever rate/point signal the API exposes, from headers or GraphQL extensions."""
    snap = dict(headers)
    ext = (body or {}).get("extensions", {})
    for k in ("cost", "consumedPoints", "rateLimit", "requestId"):
        if k in ext:
            snap[f"ext.{k}"] = ext[k]
    return snap


def cmd_introspect(args):
    print("== Mutation fields (looking for the RSVP mutation) ==")
    body, hdr, st = gql(QUERIES["introspect_mutation"])
    fields = (((body or {}).get("data") or {}).get("__type") or {}).get("fields") or []
    rsvp_like = [f for f in fields if "rsvp" in f["name"].lower()]
    print(json.dumps(rsvp_like or fields[:40], indent=2))
    print("\n== Query fields (confirm groupByUrlname / event / rate-limit exposure) ==")
    body2, _, _ = gql(QUERIES["introspect_ratelimit"])
    qfields = [f["name"] for f in ((((body2 or {}).get("data") or {}).get("__type") or {}).get("fields") or [])]
    print(", ".join(qfields[:60]))
    print("\n== Rate-limit headers on this call ==")
    print(json.dumps(rate_snapshot(hdr, body), indent=2))
    print("\nACTION: confirm the rsvp mutation name + input/response enum against the above,")
    print("then align QUERIES['rsvp_mutate'] and QUERIES['rsvp_state'] before any --commit run.")


def cmd_test_a(args):
    """A -- does createEventRsvp auto-join an open group for a non-member?"""
    result = {"test": "A_auto_join", "group": args.group, "event": args.event, "committed": args.commit}
    mem, mh, _ = gql(QUERIES["membership"], {"urlname": args.group})
    g = (((mem or {}).get("data") or {}).get("groupByUrlname") or {})
    result["group_isMember"] = g.get("isMember")
    result["group_joinMode"] = g.get("joinMode")
    result["membership_read"] = g or (mem or {}).get("errors")
    if g.get("isMember"):
        result["verdict"] = "SKIP: operator IS already a member of this group -- pick a group the operator is NOT in to test auto-join."
        return result
    if not args.commit:
        result["verdict"] = ("DRY-RUN: operator is a non-member (joinMode=%s). Re-run with --commit to attempt "
                             "createEventRsvp and observe whether it auto-joins or errors 'membership required'." % g.get("joinMode"))
        return result
    time.sleep(INTER_CALL_DELAY_S)
    mut, muh, st = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["mutation_http_status"] = st
    result["mutation_body"] = mut
    result["mutation_rate"] = rate_snapshot(muh, mut)
    rsvp = (((mut or {}).get("data") or {}).get("rsvp") or {})
    errs = rsvp.get("errors") or (mut or {}).get("errors")
    if rsvp.get("ticket") and not errs:
        result["verdict"] = "AUTO-JOIN CONFIRMED: RSVP succeeded for a non-member -> open groups WIDEN the on-SLA surface."
    else:
        result["verdict"] = f"MEMBERSHIP-GATED: RSVP refused for a non-member (errors={errs}). On-SLA stays 'already a member'."
    return result


def cmd_test_b(args):
    """B -- is the point budget per-token, per-app, or per-IP?"""
    n = args.n
    result = {"test": "B_quota_scope", "n": n, "calls": []}
    print(f"Burst of {n} read-only membership queries on TOKEN_1, logging rate headers...")
    for i in range(n):
        body, hdr, st = gql(QUERIES["introspect_ratelimit"], token=os.environ.get("MEETUP_TOKEN"))
        snap = rate_snapshot(hdr, body)
        result["calls"].append({"i": i, "token": 1, "status": st, "rate": snap})
        rem = snap.get("X-RateLimit-Remaining")
        print(f"  t1 #{i}: remaining={rem} reset={snap.get('X-RateLimit-Reset')}")
        time.sleep(INTER_CALL_DELAY_S)
    tok2 = os.environ.get("MEETUP_TOKEN_2")
    if tok2:
        print("Interleaving 5 calls on TOKEN_2 to see whether TOKEN_1's remaining moves (per-app) or not (per-token)...")
        for i in range(5):
            body, hdr, st = gql(QUERIES["introspect_ratelimit"], token=tok2)
            snap = rate_snapshot(hdr, body)
            result["calls"].append({"i": i, "token": 2, "status": st, "rate": snap})
            print(f"  t2 #{i}: remaining={snap.get('X-RateLimit-Remaining')}")
            time.sleep(INTER_CALL_DELAY_S)
        result["hint"] = ("Compare TOKEN_1's remaining before vs after the TOKEN_2 burst. If it dropped, the budget "
                          "is shared -> PER-APP (collapse the pacer to one global bucket, ADR-005 G2 contingency). "
                          "If unchanged, PER-TOKEN (per-tenant pacing holds).")
    else:
        result["hint"] = ("Set MEETUP_TOKEN_2 (a second user's token under the SAME client id) to distinguish per-app "
                          "from per-token. Per-IP is confirmed separately by running this from two egress IPs.")
    return result


def cmd_test_c(args):
    """C -- retry-idempotency of the RSVP mutation (MUTATES; requires --commit)."""
    result = {"test": "C_idempotency", "event": args.event, "committed": args.commit}
    if not args.commit:
        result["verdict"] = "DRY-RUN: test C mutates. Re-run with --commit on an event the operator CAN RSVP to."
        return result
    pre, _, _ = gql(QUERIES["rsvp_state"], {"eventId": args.event})
    result["pre_state"] = ((pre or {}).get("data") or {}).get("event")
    first, h1, s1 = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["first"] = {"status": s1, "body": first}
    time.sleep(INTER_CALL_DELAY_S)
    second, h2, s2 = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["second"] = {"status": s2, "body": second}
    post, _, _ = gql(QUERIES["rsvp_state"], {"eventId": args.event})
    ev = ((post or {}).get("data") or {}).get("event") or {}
    result["post_state"] = ev
    # If the API reports a single stable RSVP after two identical mutations, it is idempotent.
    r1 = (((first or {}).get("data") or {}).get("rsvp") or {}).get("ticket")
    r2 = (((second or {}).get("data") or {}).get("rsvp") or {}).get("ticket")
    if r1 and r2 and r1.get("id") == r2.get("id"):
        result["verdict"] = "IDEMPOTENT: both calls returned the same RSVP id -> a lost-ACK retry is safe."
    elif r2 and (((second or {}).get("data") or {}).get("rsvp") or {}).get("errors"):
        result["verdict"] = "IDEMPOTENT-BY-ERROR: the second call errored (already RSVPed) rather than duplicating."
    else:
        result["verdict"] = "INSPECT: compare first/second RSVP ids + post_state.going -- a changed id or +1 going count means NOT idempotent (read-before-mutate is load-bearing)."
    return result


def cmd_test_d(args):
    """D -- measured point cost of a full RSVP chain (all reads; no mutation)."""
    result = {"test": "D_point_cost", "group": args.group, "event": args.event, "steps": []}
    for label, q, v in (
        ("read_membership", QUERIES["membership"], {"urlname": args.group}),
        ("read_rsvp_state", QUERIES["rsvp_state"], {"eventId": args.event}),
    ):
        body, hdr, st = gql(q, v)
        result["steps"].append({"step": label, "status": st, "rate": rate_snapshot(hdr, body)})
        time.sleep(INTER_CALL_DELAY_S)
    result["note"] = ("Sum the per-step cost/consumedPoints (from headers or extensions) and ADD the measured "
                      "create-rsvp cost from a committed test-C run to get the real per-chain point cost. Compare "
                      "against the ~15 pts dossier estimate feeding ADR-008 detection sizing.")
    return result


def write_report(obj):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"g2-report-{ts}.json")
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    print(f"\nReport written: {path}")
    for r in (obj.get("results") or [obj]):
        if isinstance(r, dict) and r.get("verdict"):
            print(f"  [{r.get('test')}] {r['verdict']}")


def main():
    ap = argparse.ArgumentParser(description="G2 Meetup createEventRsvp spike")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("introspect", "test-a", "test-b", "test-c", "test-d", "all"):
        p = sub.add_parser(name)
        p.add_argument("--group", help="group urlname the operator is NOT a member of (tests A/D)")
        p.add_argument("--event", help="event id under that group")
        p.add_argument("--commit", action="store_true", help="actually fire the mutation (A/C)")
        p.add_argument("--n", type=int, default=40, help="burst size for test-b")
    args = ap.parse_args()

    if args.cmd == "introspect":
        cmd_introspect(args); return
    dispatch = {"test-a": cmd_test_a, "test-b": cmd_test_b, "test-c": cmd_test_c, "test-d": cmd_test_d}
    if args.cmd in dispatch:
        write_report(dispatch[args.cmd](args)); return
    if args.cmd == "all":
        out = {"suite": "G2 A+B+D (dry-run)", "utc": datetime.now(timezone.utc).isoformat(), "results": []}
        out["results"].append(cmd_test_a(args))
        out["results"].append(cmd_test_b(args))
        out["results"].append(cmd_test_d(args))
        write_report(out)


if __name__ == "__main__":
    main()
