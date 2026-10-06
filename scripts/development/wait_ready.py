"""Check desired replicas explicitly; Helm can accept zero ready with maxUnavailable=1."""

import argparse
import json
import os
import subprocess
import time

from events_concierge.deployment.development_targets import TARGETS

NAMESPACE = "events-concierge-dev"
CONTEXT = TARGETS["shared"].context
EXPECTED = {
    "events-concierge-" + name
    for name in [
        "api",
        "admin",
        "frontend",
        "temporal-catalog",
        "ingestion-executor",
        "account-erasure",
    ]
}


DEFERRED = {
    "events-concierge-" + name
    for name in ("temporal-transactional", "request-starter", "notifier", "change-delivery")
}
PRIVATE_EXPECTED = EXPECTED - {"events-concierge-admin"}
STORES = {"ec-dev-redis"} | {
    "ec-dev-temporal-" + name for name in ("frontend", "history", "matching", "worker")
}
PRIVATE_OPERATORS = {"events-concierge-operator-" + name for name in ("api", "frontend")}
SHARED_OPERATORS = {"events-concierge-operator-api"}


def expected_replicas(profile, *, operator=False, shared_frontend=False):
    if profile not in ("development", "private"):
        raise SystemExit("Unknown deployment profile")
    if operator and profile != "private":
        raise SystemExit("Operator readiness requires the authenticated private profile")
    if shared_frontend and (profile != "private" or not operator):
        raise SystemExit("Shared frontend readiness requires --operator and --profile private")
    expected = dict.fromkeys(PRIVATE_EXPECTED if profile == "private" else EXPECTED, 1)
    if operator:
        expected.update(
            dict.fromkeys(SHARED_OPERATORS if shared_frontend else PRIVATE_OPERATORS, 1)
        )
    return expected


def pending_deployments(items, *, profile="development", operator=False, shared_frontend=False):
    """Require every expected process, including the command executor, to finish rollout."""
    by_name = {x["metadata"]["name"]: x for x in items}
    expected = expected_replicas(profile, operator=operator, shared_frontend=shared_frontend)
    unexpected = set(by_name) - set(expected) - STORES if profile == "private" else DEFERRED
    pending = sorted(name for name in unexpected if name in by_name)
    for name, replicas in sorted(expected.items()):
        d = by_name.get(name, {})
        status = d.get("status", {})
        if (
            d.get("spec", {}).get("replicas") != replicas
            or status.get("observedGeneration", 0) < d.get("metadata", {}).get("generation", 1)
            or any(
                status.get(k, 0) != replicas
                for k in ["replicas", "updatedReplicas", "availableReplicas", "readyReplicas"]
            )
            or status.get("unavailableReplicas", 0) != 0
        ):
            pending.append(name)
    return pending


def main(*, target="shared", profile="development", operator=False, shared_frontend=False):
    expected = expected_replicas(profile, operator=operator, shared_frontend=shared_frontend)
    expected_context = TARGETS[target].context
    kubectl = os.environ.get("KUBECTL", "kubectl")
    context = subprocess.check_output(
        [kubectl, "config", "current-context"], text=True, timeout=10
    ).strip()
    if context != expected_context:
        raise SystemExit("Wrong cluster context")
    for _ in range(120):
        items = json.loads(
            subprocess.check_output(
                [
                    kubectl,
                    "--context",
                    expected_context,
                    "-n",
                    NAMESPACE,
                    "--request-timeout=20s",
                    "get",
                    "deployments",
                    "-o",
                    "json",
                ],
                timeout=30,
            )
        )["items"]
        pending = pending_deployments(
            items, profile=profile, operator=operator, shared_frontend=shared_frontend
        )
        if not pending:
            count = len(expected)
            print(f"All {count} expected deployments have their updated, available, ready replicas")
            return
        print("Waiting:", ", ".join(pending), flush=True)
        time.sleep(5)
    raise SystemExit("Application readiness timed out")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, default="shared")
    parser.add_argument("--profile", choices=("development", "private"), default="development")
    parser.add_argument("--operator", action="store_true")
    parser.add_argument(
        "--shared-frontend", action="store_true", help="Use the common consumer/admin frontend"
    )
    args = parser.parse_args()
    main(
        target=args.target,
        profile=args.profile,
        operator=args.operator,
        shared_frontend=args.shared_frontend,
    )
