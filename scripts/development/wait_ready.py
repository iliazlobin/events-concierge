"""Check desired replicas explicitly; Helm can accept zero ready with maxUnavailable=1."""

import argparse
import json
import os
import subprocess
import time

from events_concierge.deployment.development_targets import TARGETS

NAMESPACE = "events-concierge-dev"
CONTEXT = "gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev"
EXPECTED = {
    "events-concierge-" + name
    for name in [
        "api",
        "admin",
        "frontend",
        "temporal-transactional",
        "temporal-catalog",
        "ingestion-executor",
        "request-starter",
        "notifier",
        "account-erasure",
        "change-delivery",
    ]
}


def pending_deployments(items):
    """Require every expected process, including the command executor, to finish rollout."""
    by_name = {x["metadata"]["name"]: x for x in items}
    pending = []
    for name in sorted(EXPECTED):
        d = by_name.get(name, {})
        status = d.get("status", {})
        if (
            d.get("spec", {}).get("replicas") != 1
            or status.get("observedGeneration", 0) < d.get("metadata", {}).get("generation", 1)
            or any(
                status.get(k, 0) != 1
                for k in ["updatedReplicas", "availableReplicas", "readyReplicas"]
            )
        ):
            pending.append(name)
    return pending


def main(*, target="legacy"):
    expected_context = TARGETS[target].context
    kubectl = os.environ.get("KUBECTL", "kubectl")
    context = subprocess.check_output([kubectl, "config", "current-context"], text=True).strip()
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
                    "get",
                    "deployments",
                    "-o",
                    "json",
                ]
            )
        )["items"]
        pending = pending_deployments(items)
        if not pending:
            print(
                "All ten application, admin and executor deployments have one updated, available, ready replica"
            )
            return
        print("Waiting:", ", ".join(pending), flush=True)
        time.sleep(5)
    raise SystemExit("Application readiness timed out")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, default="legacy")
    main(target=parser.parse_args().target)
