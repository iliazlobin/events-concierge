"""Check desired replicas explicitly; Helm can accept zero ready with maxUnavailable=1."""

import json
import os
import subprocess
import time

kubectl = os.environ.get("KUBECTL", "kubectl")
namespace = "events-concierge-dev"
expected = {
    "events-concierge-" + name
    for name in [
        "api",
        "frontend",
        "temporal-transactional",
        "temporal-catalog",
        "request-starter",
        "notifier",
        "account-erasure",
        "change-delivery",
    ]
}
context = subprocess.check_output([kubectl, "config", "current-context"], text=True).strip()
if context != "gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev":
    raise SystemExit("Wrong cluster context")
for _ in range(120):
    items = json.loads(
        subprocess.check_output([kubectl, "-n", namespace, "get", "deployments", "-o", "json"])
    )["items"]
    by_name = {x["metadata"]["name"]: x for x in items}
    pending = []
    for name in sorted(expected):
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
    if not pending:
        print("All eight application deployments have one updated, available, ready replica")
        break
    print("Waiting:", ", ".join(pending), flush=True)
    time.sleep(5)
else:
    raise SystemExit("Application readiness timed out")
