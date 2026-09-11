#!/usr/bin/env python3
"""Fail closed on scope/cost drift in the one-node development Terraform plan."""

import argparse
import json
from pathlib import Path

PROJECT = "project-9c8cce04-f94d-40fc-aa6"
_BOOT_DISK_SIZE_GB = 30
ALLOWED = {
    "google_project_service",
    "google_service_account",
    "google_project_iam_member",
    "google_container_cluster",
    "google_container_node_pool",
    "google_artifact_registry_repository",
    "google_artifact_registry_repository_iam_member",
    "google_service_account_iam_member",
    "google_secret_manager_secret",
    "google_secret_manager_secret_iam_member",
    "google_storage_bucket",
    "google_storage_bucket_iam_member",
}
_CATALOG_MEMBER = f"serviceAccount:ec-dev-temporal-catalog@{PROJECT}.iam.gserviceaccount.com"
_CATALOG_REVOCATIONS = {
    'google_secret_manager_secret_iam_member.reader["temporal-catalog/database-url"]': {
        "type": "google_secret_manager_secret_iam_member",
        "before": {
            "project": PROJECT,
            "secret_id": f"projects/{PROJECT}/secrets/ec-dev-database-url",
            "member": _CATALOG_MEMBER,
            "role": "roles/secretmanager.secretAccessor",
        },
    },
    'google_storage_bucket_iam_member.payloads["temporal-catalog"]': {
        "type": "google_storage_bucket_iam_member",
        "before": {
            "bucket": "b/iz27-ec-dev-payloads",
            "member": _CATALOG_MEMBER,
            "role": "roles/storage.objectUser",
        },
    },
}


def _is_operator_cutover_revocation(resource):
    """Match the old catalog grants exactly; never authorize a replacement or broader deletion."""
    allowed = _CATALOG_REVOCATIONS.get(resource.get("address"))
    if (
        allowed is None
        or resource.get("mode") != "managed"
        or resource.get("type") != allowed["type"]
    ):
        return False
    change = resource.get("change", {})
    before = change.get("before")
    if (
        change.get("actions") != ["delete"]
        or "after" not in change
        or change["after"] is not None
        or not isinstance(before, dict)
        or before.get("condition") not in (None, [])
        or before.get("project", PROJECT) != PROJECT
    ):
        return False
    return all(before.get(key) == value for key, value in allowed["before"].items())


def inspect(plan, *, operator_cutover=False):  # noqa: PLR0912 - Keep boundary checks together for plan review.
    errors = []
    for r in plan.get("resource_changes", []):
        if r.get("mode") == "data":
            continue
        c = r["change"]
        cutover_revocation = operator_cutover and _is_operator_cutover_revocation(r)
        v = c["before"] if cutover_revocation else c.get("after") or {}
        kind = r["type"]
        if c["actions"] == ["no-op"]:
            continue
        if c["actions"] != ["create"] and not cutover_revocation:
            message = (
                "only additions and the exact legacy catalog IAM revocations allowed"
                if operator_cutover
                else "only initial additions allowed"
            )
            errors.append(r["address"] + ": " + message)
        if kind not in ALLOWED:
            errors.append(r["address"] + ": resource outside development scope")
        if v.get("project", PROJECT) != PROJECT:
            errors.append(r["address"] + ": wrong project")
        if kind == "google_container_cluster":
            endpoints = (v.get("control_plane_endpoints_config") or [{}])[0]
            ip = (endpoints.get("ip_endpoints_config") or [{}])[0]
            private = (v.get("private_cluster_config") or [{}])[0]
            if (
                v.get("name") != "ec-dev"
                or v.get("location") != "us-west1-a"
                or ip.get("enabled") is not False
                or not private.get("enable_private_nodes")
            ):
                errors.append(
                    "cluster must use private nodes and DNS-only administration in selected zone"
                )
            for key, suffix in [
                ("network", "/networks/iz27-dev"),
                ("subnetwork", "/subnetworks/iz27-dev-usw1"),
            ]:
                if not str(v.get(key, "")).endswith(suffix):
                    errors.append("wrong foundation " + key)
        if kind == "google_container_node_pool":
            node = (v.get("node_config") or [{}])[0]
            if (
                v.get("node_count") != 1
                or v.get("node_locations") != ["us-west1-a"]
                or v.get("autoscaling")
                or node.get("machine_type") != "e2-standard-2"
            ):
                errors.append("8 GiB gate: one fixed e2-standard-2 only")
            if node.get("disk_size_gb") != _BOOT_DISK_SIZE_GB:
                errors.append("unexpected boot disk cost")
        if kind == "google_storage_bucket" and (
            v.get("name") not in {"iz27-ec-dev-payloads", "iz27-ec-dev-backups"}
            or v.get("force_destroy")
            or not v.get("uniform_bucket_level_access")
            or v.get("public_access_prevention") != "enforced"
        ):
            errors.append("bucket boundary changed")
        if "_iam_" in kind:
            member = v.get("member", "")
            if member in {"allUsers", "allAuthenticatedUsers"} or v.get("role") in {
                "roles/owner",
                "roles/editor",
            }:
                errors.append("unsafe IAM")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Saved terraform show -json plan")
    parser.add_argument(
        "--operator-cutover",
        action="store_true",
        help="Allow only the reviewed legacy temporal-catalog IAM grant removals; requires paused writers",
    )
    args = parser.parse_args(argv)
    with args.plan.open() as source:
        errors = inspect(json.load(source), operator_cutover=args.operator_cutover)
    print("\n".join(errors) if errors else "Development plan scope passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
