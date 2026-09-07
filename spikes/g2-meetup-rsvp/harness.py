#!/usr/bin/env python3
"""
G2 spike harness -- Meetup createEventRsvp behavior probe.

Answers the four G2 questions the requirements mark DESIGN-BLOCKING for any
autonomous-lane SLA (requirements.md section 8, ADR-005/ADR-008):

  A. Does the RSVP mutation AUTO-JOIN an open group for a non-member, or does it
     require prior membership? (Decides whether "open group" widens the on-SLA
     surface beyond "groups the user is already in".)
  B. Does the live API expose/enforce a rate or point budget, and is any observed
     budget scoped per-OAUTH-TOKEN, per-APP (client id), or per-IP? (Meetup's
     current help and GraphQL guide are not consistent enough to treat a fixed
     historical number as a launch fact.)
  C. Is the RSVP mutation retry-IDEMPOTENT? (Backstops the FR-5.3 read-before-
     mutate guard: a lost-ACK retry must not create a duplicate RSVP.)
  D. What is the real per-RSVP-chain point cost (read-membership + read-rsvp-state
     + create-rsvp)? (The ~15 pts figure in the dossiers is an UNPUBLISHED
     estimate; ADR-008 sizing depends on the measured value.)

DESIGN POSTURE (why this is a legitimate probe, not abuse):
  - It runs against the OPERATOR's own Meetup account via the sanctioned GraphQL
    API -- exactly the modality the product uses in production.
  - It is DRY-RUN by default: no mutation fires without --commit.
  - N is tiny, a 429 stops the run at the provider boundary, and there is a fixed
    inter-call delay (human cadence). No block circumvention or identity rotation.
  - Test A and C, when committed, may leave the account RSVPed to a real event;
    withdraw through ordinary Meetup controls after recording the result.

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
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests  (see requirements.txt)")

GQL_ENDPOINT = "https://api.meetup.com/gql-ext"
MIN_INTER_CALL_DELAY_S = 1.0
MAX_INTER_CALL_DELAY_S = 30.0
RATE_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                "Retry-After", "X-RateLimit-Used", "X-RateLimit-Cost")
MAX_READ_ONLY_PROBES = 100
# Add a code only when an authoritative, versioned provider contract defines its semantics.
# A generic second-call error is not evidence of idempotency.
DOCUMENTED_ALREADY_RSVPED_CODES = frozenset()
DOCUMENTED_MEMBERSHIP_REQUIRED_CODES = frozenset()

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
    if len(tok.encode("utf-8")) > 8192 or any(ord(char) < 32 for char in tok):
        sys.exit("Meetup token must be a bounded single-line value")
    r = requests.post(
        GQL_ENDPOINT,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        data=json.dumps({"query": query, "variables": variables or {}}),
        timeout=30,
        allow_redirects=False,
    )
    if 300 <= r.status_code < 400:
        sys.exit(f"Meetup endpoint returned redirect HTTP {r.status_code}; token was not forwarded")
    # A field probe must never push through a throttle or rotate identity. Stop and
    # let the operator honor the provider's stated recovery window before rerunning.
    if r.status_code == 429:
        reset = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset")
        sys.exit(f"[429] throttled; provider recovery signal={reset!r}; run stopped, not bypassed")
    try:
        body = r.json()
    except Exception:
        body = {"_invalid_json": True}
    return body, {h: r.headers.get(h) for h in RATE_HEADERS if r.headers.get(h)}, r.status_code


def rate_snapshot(headers, body):
    """Pull whatever rate/point signal the API exposes, from headers or GraphQL extensions."""
    snap = {
        key: safe
        for key, value in headers.items()
        if (safe := _safe_scalar(value)) is not None
    }
    ext = (body or {}).get("extensions", {})
    for k in ("cost", "consumedPoints", "rateLimit"):
        if k in ext:
            safe = _safe_rate_value(ext[k])
            if safe is not None:
                snap[f"ext.{k}"] = safe
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
    result = {
        "test": "A_auto_join",
        "fixture_ref": fixture_ref(args.group, args.event),
        "committed": args.commit,
    }
    mem, _, membership_status = gql(QUERIES["membership"], {"urlname": args.group})
    membership = _membership_observation(mem, membership_status)
    if membership is None:
        result["verdict"] = (
            "INCONCLUSIVE: the pre-mutation membership query did not return a valid authoritative "
            "membership object."
        )
        return result
    result["group_isMember"] = membership["is_member"]
    result["group_joinMode"] = membership["join_mode"]
    result["membership_error_codes"] = error_codes(mem)
    if membership["is_member"]:
        result["verdict"] = "SKIP: operator IS already a member of this group -- pick a group the operator is NOT in to test auto-join."
        return result
    if not args.commit:
        result["verdict"] = (
            "DRY-RUN: operator is a non-member (sanitized joinMode=%s). Re-run with --commit "
            "to attempt createEventRsvp and observe the post-mutation membership state."
            % membership["join_mode"]
        )
        return result
    time.sleep(_inter_call_delay())
    mut, muh, st = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["mutation_http_status"] = st
    result["mutation_rate"] = rate_snapshot(muh, mut)
    rsvp = (((mut or {}).get("data") or {}).get("rsvp") or {})
    errs = rsvp.get("errors") or (mut or {}).get("errors")
    result["ticket_present"] = bool(rsvp.get("ticket"))
    result["mutation_error_codes"] = error_codes({"errors": errs})
    time.sleep(_inter_call_delay())
    post_mem, _, post_membership_status = gql(
        QUERIES["membership"], {"urlname": args.group}
    )
    post_membership = _membership_observation(post_mem, post_membership_status)
    if post_membership is None:
        result["verdict"] = (
            "INCONCLUSIVE: the mutation ran but the authoritative post-mutation membership read "
            "was unavailable."
        )
    elif st == 200 and rsvp.get("ticket") and not errs and post_membership["is_member"]:
        result["verdict"] = (
            "AUTO-JOIN CONFIRMED: RSVP succeeded for a verified non-member and the post-state "
            "shows membership."
        )
    elif st == 200 and rsvp.get("ticket") and not errs and not post_membership["is_member"]:
        result["verdict"] = (
            "NON-MEMBER RSVP CONFIRMED: RSVP succeeded while post-state remains non-member; the "
            "autonomous surface widens, but the provider did not auto-join the account."
        )
    elif (
        st == 200
        and set(result["mutation_error_codes"]) & DOCUMENTED_MEMBERSHIP_REQUIRED_CODES
        and not post_membership["is_member"]
    ):
        result["verdict"] = (
            "MEMBERSHIP-GATED: an authoritative documented membership-required code and unchanged "
            "non-member post-state establish the gate."
        )
    else:
        result["verdict"] = (
            "INCONCLUSIVE: no ticket plus undocumented/generic errors cannot establish a "
            "membership gate."
        )
    return result


def cmd_test_b(args):
    """B -- is the point budget per-token, per-app, or per-IP?"""
    n = args.n
    if not 1 <= n <= MAX_READ_ONLY_PROBES:
        raise SystemExit(f"--n must be between 1 and {MAX_READ_ONLY_PROBES}")
    result = {"test": "B_quota_scope", "n": n, "calls": []}
    print(f"Burst of {n} read-only membership queries on TOKEN_1, logging rate headers...")
    for i in range(n):
        body, hdr, st = gql(QUERIES["introspect_ratelimit"], token=os.environ.get("MEETUP_TOKEN"))
        snap = rate_snapshot(hdr, body)
        result["calls"].append({"i": i, "token": 1, "status": st, "rate": snap})
        rem = snap.get("X-RateLimit-Remaining")
        print(f"  t1 #{i}: remaining={rem} reset={snap.get('X-RateLimit-Reset')}")
        time.sleep(_inter_call_delay())
    tok2 = os.environ.get("MEETUP_TOKEN_2")
    if tok2:
        print("Calling TOKEN_2, then re-checking TOKEN_1 inside the same observed reset window...")
        for i in range(5):
            body, hdr, st = gql(QUERIES["introspect_ratelimit"], token=tok2)
            snap = rate_snapshot(hdr, body)
            result["calls"].append({"i": i, "token": 2, "status": st, "rate": snap})
            print(f"  t2 #{i}: remaining={snap.get('X-RateLimit-Remaining')}")
            time.sleep(_inter_call_delay())
        body, hdr, st = gql(
            QUERIES["introspect_ratelimit"], token=os.environ.get("MEETUP_TOKEN")
        )
        snap = rate_snapshot(hdr, body)
        result["calls"].append({"i": n, "token": 1, "phase": "post_token_2", "status": st, "rate": snap})
        print(f"  t1 post-token2: remaining={snap.get('X-RateLimit-Remaining')} reset={snap.get('X-RateLimit-Reset')}")
        result["hint"] = ("Compare TOKEN_1's last pre-token2 and post-token2 observations only when they share a "
                          "reset window. A provider-exposed shared decrement supports PER-APP; an independent "
                          "counter supports PER-TOKEN. Missing counters are INCONCLUSIVE, not proof of no limit.")
    else:
        result["hint"] = ("Set MEETUP_TOKEN_2 (a second user's token under the SAME client id) to distinguish per-app "
                          "from per-token. Per-IP is confirmed separately by running this from two egress IPs.")
    return result


def cmd_test_c(args):
    """C -- retry-idempotency of the RSVP mutation (MUTATES; requires --commit)."""
    result = {
        "test": "C_idempotency",
        "fixture_ref": fixture_ref(None, args.event),
        "committed": args.commit,
    }
    if not args.commit:
        result["verdict"] = "DRY-RUN: test C mutates. Re-run with --commit on an event the operator CAN RSVP to."
        return result
    pre, _, _ = gql(QUERIES["rsvp_state"], {"eventId": args.event})
    pre_event = ((pre or {}).get("data") or {}).get("event") or {}
    result["pre_state"] = event_state(pre_event)
    if result["pre_state"]["self_rsvp_present"]:
        result["verdict"] = (
            "SKIP: the fixture already had a self RSVP; withdraw through ordinary provider "
            "controls and use a clean fixture."
        )
        return result
    first, h1, s1 = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["first"] = mutation_state(s1, first, h1)
    time.sleep(_inter_call_delay())
    second, h2, s2 = gql(QUERIES["rsvp_mutate"], {"eventId": args.event, "response": "YES"})
    result["second"] = mutation_state(s2, second, h2)
    post, _, _ = gql(QUERIES["rsvp_state"], {"eventId": args.event})
    ev = ((post or {}).get("data") or {}).get("event") or {}
    result["post_state"] = event_state(ev)
    # A generic error may mean validation, authorization, throttling, or a transient failure.
    # Only stable identity plus a compatible post-state is direct evidence of idempotency.
    r1 = (((first or {}).get("data") or {}).get("rsvp") or {}).get("ticket")
    r2 = (((second or {}).get("data") or {}).get("rsvp") or {}).get("ticket")
    second_rsvp = (((second or {}).get("data") or {}).get("rsvp") or {})
    second_errors = error_codes(second) or error_codes({"errors": second_rsvp.get("errors")})
    same_ticket = bool(r1 and r2 and r1.get("id") == r2.get("id"))
    result["same_ticket_identity"] = same_ticket
    compatible_post_state = _compatible_single_rsvp(result["pre_state"], result["post_state"])
    documented_duplicate = bool(set(second_errors) & DOCUMENTED_ALREADY_RSVPED_CODES)
    if same_ticket and compatible_post_state:
        result["verdict"] = (
            "IDEMPOTENT: both calls returned the same RSVP identity and the aggregate post-state "
            "is compatible with one RSVP."
        )
    elif documented_duplicate and compatible_post_state:
        result["verdict"] = (
            "IDEMPOTENT: the second call returned an authoritative documented already-RSVPed "
            "code and the aggregate post-state is compatible with one RSVP."
        )
    elif second_errors:
        result["verdict"] = (
            "INCONCLUSIVE: the second call returned only undocumented/generic error codes; an "
            "error alone does not prove idempotency."
        )
    else:
        result["verdict"] = (
            "INCONCLUSIVE OR NOT IDEMPOTENT: stable RSVP identity plus a one-RSVP post-state was "
            "not established; keep read-before-mutate load-bearing and G2 open."
        )
    return result


def cmd_test_d(args):
    """D -- measured point cost of a full RSVP chain (all reads; no mutation)."""
    result = {
        "test": "D_point_cost",
        "fixture_ref": fixture_ref(args.group, args.event),
        "steps": [],
    }
    for label, q, v in (
        ("read_membership", QUERIES["membership"], {"urlname": args.group}),
        ("read_rsvp_state", QUERIES["rsvp_state"], {"eventId": args.event}),
    ):
        body, hdr, st = gql(q, v)
        result["steps"].append({"step": label, "status": st, "rate": rate_snapshot(hdr, body)})
        time.sleep(_inter_call_delay())
    result["note"] = ("Sum the per-step cost/consumedPoints (from headers or extensions) and ADD the measured "
                      "create-rsvp cost from a committed test-C run to get the real per-chain point cost. Compare "
                      "against the ~15 pts dossier estimate feeding ADR-008 detection sizing.")
    return result


def write_report(obj):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"g2-report-{ts}.json")
    with open(path, "x") as f:
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
    if args.cmd in {"test-a", "test-d", "all"} and (not args.group or not args.event):
        ap.error(f"{args.cmd} requires both --group and --event")
    if args.cmd == "test-c" and not args.event:
        ap.error("test-c requires --event")
    if args.commit and args.cmd not in {"test-a", "test-c"}:
        ap.error(f"{args.cmd} is evidence-only and does not accept --commit")
    if args.cmd in dispatch:
        write_report(dispatch[args.cmd](args)); return
    if args.cmd == "all":
        out = {"suite": "G2 A+B+D (dry-run)", "utc": datetime.now(timezone.utc).isoformat(), "results": []}
        out["results"].append(cmd_test_a(args))
        out["results"].append(cmd_test_b(args))
        out["results"].append(cmd_test_d(args))
        write_report(out)


def fixture_ref(group, event):
    """Correlate a controlled fixture without persisting its provider identifiers."""
    salt = os.environ.get("MEETUP_REPORT_SALT")
    if salt is None or len(salt.encode("utf-8")) < 32:
        raise SystemExit("MEETUP_REPORT_SALT must contain at least 32 bytes")
    material = f"{group or ''}\0{event or ''}".encode()
    return hmac.new(salt.encode(), material, hashlib.sha256).hexdigest()


def error_codes(body):
    """Keep stable provider codes only; never persist raw errors or response bodies."""
    errors = (body or {}).get("errors") or []
    if not isinstance(errors, list):
        return []
    return sorted(
        {
            str(item.get("code"))[:80]
            for item in errors
            if isinstance(item, dict) and item.get("code") is not None
        }
    )


def event_state(event):
    """Project only fields required for the idempotency verdict."""
    return {
        "going": event.get("going") if isinstance(event, dict) else None,
        "rsvp_state": event.get("rsvpState") if isinstance(event, dict) else None,
        "self_rsvp_present": bool(event.get("selfRsvp")) if isinstance(event, dict) else False,
    }


def mutation_state(status, body, headers):
    rsvp = (((body or {}).get("data") or {}).get("rsvp") or {})
    return {
        "http_status": status,
        "ticket_present": bool(rsvp.get("ticket")),
        "error_codes": error_codes(body) or error_codes({"errors": rsvp.get("errors")}),
        "rate": rate_snapshot(headers, body),
    }


def _compatible_single_rsvp(pre_state, post_state):
    """Require one visible self-RSVP and reject an aggregate count jump larger than one."""
    if not post_state.get("self_rsvp_present"):
        return False
    before = pre_state.get("going")
    after = post_state.get("going")
    if isinstance(before, bool) or isinstance(after, bool):
        return False
    if isinstance(before, int) and isinstance(after, int):
        return 0 <= after - before <= 1
    return True


def _membership_observation(body, status):
    if status != 200 or error_codes(body):
        return None
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        return None
    group = data.get("groupByUrlname")
    if not isinstance(group, dict) or not isinstance(group.get("isMember"), bool):
        return None
    return {
        "is_member": group["isMember"],
        "join_mode": _safe_code(group.get("joinMode")),
    }


def _safe_code(value):
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if not all(char.isascii() and (char.isalnum() or char in "_-.") for char in value):
        return None
    return value


def _safe_scalar(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return _safe_code(value)


def _safe_rate_value(value):
    scalar = _safe_scalar(value)
    if scalar is not None:
        return scalar
    if not isinstance(value, dict):
        return None
    allowed = {
        "limit",
        "remaining",
        "resetAt",
        "cost",
        "requestedQueryCost",
        "actualQueryCost",
        "maximumAvailable",
        "currentlyAvailable",
        "restoreRate",
    }
    result = {}
    for key, item in value.items():
        if key in allowed:
            safe = _safe_scalar(item)
            if safe is not None:
                result[key] = safe
    return result or None


def _inter_call_delay():
    try:
        delay = float(os.environ.get("MEETUP_DELAY_S", "1.5"))
    except ValueError as error:
        raise SystemExit("MEETUP_DELAY_S must be numeric") from error
    if not MIN_INTER_CALL_DELAY_S <= delay <= MAX_INTER_CALL_DELAY_S:
        raise SystemExit(
            f"MEETUP_DELAY_S must be between {MIN_INTER_CALL_DELAY_S} and "
            f"{MAX_INTER_CALL_DELAY_S} seconds"
        )
    return delay


if __name__ == "__main__":
    main()
