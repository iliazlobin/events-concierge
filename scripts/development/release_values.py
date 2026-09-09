#!/usr/bin/env python3
"""Write non-secret Helm identity/image bindings from Terraform outputs."""

import argparse
import json
import pathlib
import re
import subprocess

import yaml

p = argparse.ArgumentParser()
p.add_argument("--app-image", required=True)
p.add_argument("--web-image", required=True)
p.add_argument("--output", required=True)
p.add_argument("--revision", required=True, help="Backend image source commit")
a = p.parse_args()
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
        [tf, "-chdir=" + str(root / "infra/terraform/environments/development"), "output", "-json"]
    )
)
accounts = out["service_accounts"]["value"]
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
print("Wrote non-secret development release values")
