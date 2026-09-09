#!/usr/bin/env python3
"""Fail closed on scope/cost drift in the one-node development Terraform plan."""

import json
import sys

PROJECT = "project-9c8cce04-f94d-40fc-aa6"
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


def inspect(plan):
    errors = []
    for r in plan.get("resource_changes", []):
        if r.get("mode") == "data":
            continue
        c = r["change"]
        v = c.get("after") or {}
        kind = r["type"]
        if c["actions"] == ["no-op"]:
            continue
        if c["actions"] != ["create"]:
            errors.append(r["address"] + ": only initial additions allowed")
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
            if node.get("disk_size_gb") != 30:
                errors.append("unexpected boot disk cost")
        if kind == "google_storage_bucket":
            if (
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


if __name__ == "__main__":
    e = inspect(json.load(open(sys.argv[1])))
    print("\n".join(e) if e else "Development plan scope passed")
    sys.exit(bool(e))
