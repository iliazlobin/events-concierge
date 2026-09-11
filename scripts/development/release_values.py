#!/usr/bin/env python3
"""Write non-secret Helm identity/image bindings from Terraform outputs."""

import argparse
import json
import pathlib
import re
import subprocess

import yaml

from events_concierge.deployment.development_targets import TARGETS

p = argparse.ArgumentParser()
p.add_argument("--app-image", required=True)
p.add_argument("--web-image", required=True)
p.add_argument("--output", required=True)
p.add_argument("--revision", required=True, help="Backend image source commit")
p.add_argument("--target", choices=TARGETS, default="legacy")
a = p.parse_args()
target = TARGETS[a.target]
if not re.fullmatch("[0-9a-f]{40}", a.revision):
    raise SystemExit("Full backend source commit required")


def image(value):
    repo, sep, digest = value.partition("@")
    if not sep or not re.fullmatch("sha256:[0-9a-f]{64}", digest):
        raise SystemExit("Immutable image digest required")
    return {"repository": repo, "digest": digest, "pullPolicy": "IfNotPresent"}


root = pathlib.Path(__file__).resolve().parents[2]
tf = __import__("os").environ.get("TERRAFORM", "terraform")
out = json.loads(
    subprocess.check_output(
        [
            tf,
            "-chdir=" + str(root / "infra/terraform/environments" / target.terraform_root),
            "output",
            "-json",
        ]
    )
)
accounts = out["service_accounts"]["value"]
if not accounts or any(
    not email.endswith("@" + target.project + ".iam.gserviceaccount.com")
    for email in accounts.values()
):
    raise SystemExit("Terraform workload identities do not match the selected destination")
if out["cluster"]["value"] != target.cluster or out["namespace"]["value"] != "events-concierge-dev":
    raise SystemExit("Terraform cluster or namespace does not match the selected destination")
v = {
    "global": {
        "appImage": image(a.app_image),
        "frontendImage": image(a.web_image),
        "releaseRevision": a.revision,
    },
    "serviceAccounts": {
        k: {"name": "events-concierge-" + k, "gcpServiceAccount": email}
        for k, email in accounts.items()
    },
}
pathlib.Path(a.output).write_text(yaml.safe_dump(v, sort_keys=False))
print("Wrote non-secret " + a.target + " development release values")
