#!/usr/bin/env python3
"""Reject shared-platform ownership, replacement, access and data drift in app landing plans."""

import argparse
import json
from pathlib import Path

PROJECT = "iz27-platform-dev"
_SOFT_DELETE_SECONDS = 604800
NAMES = frozenset(
    {
        "api",
        "frontend",
        "temporal-transactional",
        "temporal-catalog",
        "request-starter",
        "notifier",
        "account-erasure",
        "change-delivery",
        "handoff-expiry",
        "lifecycle-invariants",
        "catalog-jobs",
        "migration",
        "stores",
        "development-admin",
        "ingestion-executor",
    }
)
CATALOG = frozenset({"ingestion-executor", "temporal-catalog"})
CONSUMERS = NAMES - CATALOG - {"frontend", "stores", "migration", "development-admin"}
SECRETS = {
    "stores": {"postgres-admin", "temporal-postgres-admin", "redis-password"},
    "migration": {
        "migration-url",
        "app-role-password",
        "operator-role-password",
        "ingestion-executor-role-password",
    },
    "development-admin": {"operator-database-url", "database-url", "redis-url"},
    **{name: {"ingestion-executor-database-url", "redis-url"} for name in CATALOG},
    **{name: {"database-url", "redis-url"} for name in CONSUMERS},
}
SECRET_NAMES = set().union(*SECRETS.values())
BUCKETS = {
    "iz27-platform-dev-ec-payloads",
    "iz27-platform-dev-ec-backups",
    "iz27-platform-dev-ec-state",
}
ALLOWED = {
    "google_service_account",
    "google_service_account_iam_member",
    "google_artifact_registry_repository",
    "google_artifact_registry_repository_iam_member",
    "google_secret_manager_secret",
    "google_secret_manager_secret_iam_member",
    "google_storage_bucket",
    "google_storage_bucket_iam_member",
}


def _workload(member):
    return next(
        (
            name
            for name in NAMES
            if member == f"serviceAccount:ec-dev-{name}@{PROJECT}.iam.gserviceaccount.com"
        ),
        None,
    )


def _secret_name(value):
    return next(
        (
            name
            for name in SECRET_NAMES
            if value
            in {
                f"ec-dev-{name}",
                f"projects/{PROJECT}/secrets/ec-dev-{name}",
            }
        ),
        None,
    )


def _safe_resource(kind, value):  # noqa: PLR0911, PLR0912 - Scope review stays in one allowlist.
    if kind not in ALLOWED:
        return False
    if "project" in value and value["project"] != PROJECT:
        return False
    if kind == "google_service_account":
        return value.get("project") == PROJECT and value.get("account_id") in {
            f"ec-dev-{n}" for n in NAMES
        }
    if kind == "google_service_account_iam_member":
        return (
            value.get("role") == "roles/iam.workloadIdentityUser"
            and any(
                value.get("service_account_id")
                == f"projects/{PROJECT}/serviceAccounts/ec-dev-{name}@{PROJECT}.iam.gserviceaccount.com"
                and value.get("member")
                == f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-{name}]"
                for name in NAMES
            )
            and not value.get("condition")
        )
    if kind.startswith("google_artifact_registry_repository"):
        valid = value.get("project") == PROJECT and value.get("location") == "us-west1"
        if kind.endswith("_iam_member"):
            return (
                valid
                and value.get("repository")
                in {"ec-dev", f"projects/{PROJECT}/locations/us-west1/repositories/ec-dev"}
                and value.get("member")
                == f"serviceAccount:platform-dev-node@{PROJECT}.iam.gserviceaccount.com"
                and value.get("role") == "roles/artifactregistry.reader"
                and not value.get("condition")
            )
        return valid and value.get("repository_id") == "ec-dev" and value.get("format") == "DOCKER"
    if kind.startswith("google_secret_manager_secret"):
        secret = _secret_name(value.get("secret_id"))
        if value.get("project") != PROJECT or secret is None:
            return False
        if kind.endswith("_iam_member"):
            return (
                secret in SECRETS.get(_workload(value.get("member")), set())
                and value.get("role") == "roles/secretmanager.secretAccessor"
                and not value.get("condition")
            )
        return True
    if kind == "google_storage_bucket":
        return (
            value.get("project") == PROJECT
            and value.get("name") in BUCKETS
            and value.get("location") == "US-WEST1"
            and value.get("force_destroy") is False
            and value.get("uniform_bucket_level_access") is True
            and value.get("public_access_prevention") == "enforced"
            and (value.get("versioning") or [{}])[0].get("enabled") is True
            and (value.get("soft_delete_policy") or [{}])[0].get("retention_duration_seconds")
            == _SOFT_DELETE_SECONDS
        )
    if kind == "google_storage_bucket_iam_member":
        name = _workload(value.get("member"))
        if (
            value.get("bucket")
            not in {"iz27-platform-dev-ec-payloads", "b/iz27-platform-dev-ec-payloads"}
            or value.get("role") != "roles/storage.objectUser"
        ):
            return False
        if name in CATALOG or name == "development-admin":
            prefix = "catalog" if name in CATALOG else "claim-check"
            return (
                len(value.get("condition") or []) == 1
                and value["condition"][0].get("expression")
                == f"resource.name.startsWith('projects/_/buckets/iz27-platform-dev-ec-payloads/objects/events-concierge/{prefix}/v1/')"
            )
        return name in CONSUMERS and not value.get("condition")
    return False


def inspect(plan):
    errors = []
    for resource in plan.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        change = resource.get("change", {})
        value = change.get("after")
        if resource.get("mode") != "managed" or change.get("actions") not in (
            ["create"],
            ["no-op"],
        ):
            errors.append(
                f"{resource.get('address')}: only app additions or unchanged resources allowed"
            )
        if not isinstance(value, dict) or not _safe_resource(resource.get("type"), value):
            errors.append(
                f"{resource.get('address')}: outside shared-development application boundary"
            )
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Saved terraform show -json application plan")
    args = parser.parse_args(argv)
    errors = inspect(json.loads(args.plan.read_text()))
    print("\n".join(errors) if errors else "Shared-development application plan scope passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
