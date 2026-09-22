#!/usr/bin/env python3
"""Initialize dev-only random credentials in Secret Manager and project store copies to K8s.
No values are accepted from Git, printed, or written to Terraform state. Existing versions are reused.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
from urllib.parse import quote

from events_concierge.deployment.development_targets import TARGETS

PROJECT = TARGETS["shared"].project
ACCOUNT = "iliazlobin27@gmail.com"
NS = "events-concierge-dev"
CONTEXT = TARGETS["shared"].context


def gc(*args, input=None, check=True):
    return subprocess.run(
        ["gcloud", *args, "--account=" + ACCOUNT, "--project=" + PROJECT, "--quiet"],
        input=input,
        capture_output=True,
        check=check,
    )


def read_or_create(name, factory):
    versions = json.loads(gc("secrets", "versions", "list", name, "--format=json").stdout)
    if versions:
        # A disabled/destroyed version is still an existing credential. Never turn a
        # bootstrap retry into a rotation, or create version 2 while Helm pins version 1.
        if (
            len(versions) != 1
            or versions[0]["name"].split("/")[-1] != "1"
            or versions[0].get("state") != "ENABLED"
        ):
            raise SystemExit("Secret rotation requires an explicit pinned-version release")
        return gc("secrets", "versions", "access", "1", "--secret=" + name).stdout.decode()
    value = factory()
    gc("secrets", "versions", "add", name, "--data-file=-", input=value.encode())
    return value


def initialize_credentials(*, project_to_kubernetes=False, target="shared"):
    global PROJECT, CONTEXT
    destination = TARGETS[target]
    PROJECT, CONTEXT = destination.project, destination.context
    kubectl = os.environ.get("KUBECTL", "kubectl")
    if project_to_kubernetes:
        # Validate the target before either Secret Manager or Kubernetes mutations.
        context = subprocess.check_output(
            [kubectl, "config", "current-context"], text=True, timeout=10
        ).strip()
        if context != CONTEXT:
            raise SystemExit("Select the dedicated development cluster context first")
    vals = {}
    for key in [
        "postgres-admin",
        "temporal-postgres-admin",
        "app-role-password",
        "redis-password",
        "operator-role-password",
        "ingestion-executor-role-password",
    ]:
        vals[key] = read_or_create("ec-dev-" + key, lambda: secrets.token_hex(32))
    urls = {
        "database-url": "postgresql+psycopg://ec_app:"
        + quote(vals["app-role-password"], safe="")
        + "@ec-dev-application-postgres:5432/events",
        "migration-url": "postgresql+psycopg://ec_owner:"
        + quote(vals["postgres-admin"], safe="")
        + "@ec-dev-application-postgres:5432/events",
        "redis-url": "redis://:" + quote(vals["redis-password"], safe="") + "@ec-dev-redis:6379/0",
        "operator-database-url": "postgresql+psycopg://ec_dev_operator:"
        + quote(vals["operator-role-password"], safe="")
        + "@ec-dev-application-postgres:5432/events",
        "ingestion-executor-database-url": "postgresql+psycopg://ec_dev_ingestion:"
        + quote(vals["ingestion-executor-role-password"], safe="")
        + "@ec-dev-application-postgres:5432/events",
    }
    for key, value in urls.items():
        actual = read_or_create("ec-dev-" + key, lambda v=value: v)
        if actual != value:
            raise SystemExit(
                "Existing secret references differ; review rotation instead of overwriting"
            )
    if project_to_kubernetes:
        for name, key in [
            ("ec-dev-application-postgres", "postgres-admin"),
            ("ec-dev-temporal-postgres", "temporal-postgres-admin"),
            ("ec-dev-redis", "redis-password"),
        ]:
            document = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NS},
                "type": "Opaque",
                "data": {"password": base64.b64encode(vals[key].encode()).decode()},
            }
            result = subprocess.run(
                [
                    kubectl,
                    "--context",
                    CONTEXT,
                    "-n",
                    NS,
                    "apply",
                    "--server-side",
                    "--field-manager=ec-dev-secrets",
                    "-f",
                    "-",
                ],
                input=json.dumps(document).encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if result.returncode:
                raise SystemExit(
                    "Development store credential projection failed; values not displayed"
                )
    print("Development credentials initialized; values not displayed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-to-kubernetes", action="store_true")
    parser.add_argument("--target", choices=TARGETS, default="shared")
    args = parser.parse_args()
    initialize_credentials(project_to_kubernetes=args.project_to_kubernetes, target=args.target)


if __name__ == "__main__":
    main()
