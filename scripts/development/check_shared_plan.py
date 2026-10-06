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
IDENTITY_NAMES = NAMES | {"operator-api"}
CATALOG = frozenset({"ingestion-executor", "temporal-catalog"})
CONSUMERS = NAMES - CATALOG - {"frontend", "stores", "migration", "development-admin"}
SECRETS = {
    "operator-api": {"operator-database-url"},
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
TLS_RECOVERY_NAMES = frozenset(
    f"ec-dev-{store}-ca-recovery-g1" for store in ("postgres", "redis", "temporal")
)
TLS_RECOVERY_ADDRESSES = frozenset(
    f'google_secret_manager_secret.tls_recovery["{store}"]'
    for store in ("postgres", "redis", "temporal")
)
MEDIA_BUCKET = "iz27-platform-dev-ec-media"
MEDIA_WORKLOADS = frozenset({"api", "development-admin", "account-erasure"})
BUCKETS = {
    "iz27-platform-dev-ec-payloads",
    "iz27-platform-dev-ec-backups",
    "iz27-platform-dev-ec-state",
    MEDIA_BUCKET,
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
OPERATOR_EMAIL = f"ec-dev-operator-api@{PROJECT}.iam.gserviceaccount.com"
OPERATOR_RESOURCES = {
    "google_service_account.operator_api": (
        "google_service_account",
        {"project": PROJECT, "account_id": "ec-dev-operator-api"},
    ),
    "google_service_account_iam_member.operator_api_workload": (
        "google_service_account_iam_member",
        {
            "service_account_id": f"projects/{PROJECT}/serviceAccounts/{OPERATOR_EMAIL}",
            "role": "roles/iam.workloadIdentityUser",
            "member": f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-operator-api]",
        },
    ),
    "google_secret_manager_secret_iam_member.operator_api_database": (
        "google_secret_manager_secret_iam_member",
        {
            "project": PROJECT,
            "role": "roles/secretmanager.secretAccessor",
            "member": f"serviceAccount:{OPERATOR_EMAIL}",
        },
    ),
}


def _workload(member):
    return next(
        (
            name
            for name in IDENTITY_NAMES
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


def _tls_recovery_name(value):
    return next(
        (
            name
            for name in TLS_RECOVERY_NAMES
            if value in {name, f"projects/{PROJECT}/secrets/{name}"}
        ),
        None,
    )


def _safe_tls_recovery(value):
    replication = value.get("replication") or []
    return (
        value.get("project") == PROJECT
        and value.get("labels") == {"purpose": "ca-recovery", "generation": "1"}
        and value.get("version_destroy_ttl") == "2592000s"
        and not value.get("expire_time")
        and not value.get("ttl")
        and len(replication) == 1
        and len(replication[0].get("auto") or []) == 1
        and not replication[0].get("user_managed")
        and not any(key in value for key in ("secret_data", "payload", "ca_private_key_pem"))
    )


def _safe_resource(kind, value):  # noqa: PLR0911, PLR0912 - Scope review stays in one allowlist.
    if kind not in ALLOWED:
        return False
    if "project" in value and value["project"] != PROJECT:
        return False
    if kind == "google_service_account":
        return value.get("project") == PROJECT and value.get("account_id") in {
            f"ec-dev-{n}" for n in IDENTITY_NAMES
        }
    if kind == "google_service_account_iam_member":
        return (
            value.get("role") == "roles/iam.workloadIdentityUser"
            and any(
                value.get("service_account_id")
                == f"projects/{PROJECT}/serviceAccounts/ec-dev-{name}@{PROJECT}.iam.gserviceaccount.com"
                and value.get("member")
                == f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-{name}]"
                for name in IDENTITY_NAMES
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
        if _tls_recovery_name(value.get("secret_id")):
            return kind == "google_secret_manager_secret" and _safe_tls_recovery(value)
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
        media = value.get("name") == MEDIA_BUCKET
        return (
            value.get("project") == PROJECT
            and value.get("name") in BUCKETS
            and value.get("location") == "US-WEST1"
            and value.get("force_destroy") is False
            and value.get("uniform_bucket_level_access") is True
            and value.get("public_access_prevention") == "enforced"
            and (value.get("versioning") or [{}])[0].get("enabled") is (not media)
            and (value.get("soft_delete_policy") or [{}])[0].get("retention_duration_seconds")
            == (0 if media else _SOFT_DELETE_SECONDS)
            and (
                not media
                or (
                    not value.get("retention_policy")
                    and not value.get("default_event_based_hold")
                    and not value.get("lifecycle_rule")
                )
            )
        )
    if kind == "google_storage_bucket_iam_member":
        name = _workload(value.get("member"))
        if value.get("bucket") in {MEDIA_BUCKET, f"b/{MEDIA_BUCKET}"}:
            return (
                name in MEDIA_WORKLOADS
                and value.get("role")
                in {"roles/storage.objectUser", "roles/storage.legacyBucketReader"}
                and not value.get("condition")
            )
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


def _operator_prerequisite_errors(resources):
    errors = []
    seen = set()
    for resource in resources:
        if resource.get("mode") != "managed":
            continue
        address = resource.get("address")
        change = resource.get("change", {})
        if address not in OPERATOR_RESOURCES:
            if change.get("actions") != ["no-op"]:
                errors.append(f"{address}: operator prerequisites cannot change existing resources")
            continue
        seen.add(address)
        kind, required = OPERATOR_RESOURCES[address]
        value = change.get("after")
        if not isinstance(value, dict):
            value = {}
        if resource.get("type") != kind or any(value.get(k) != v for k, v in required.items()):
            errors.append(f"{address}: wrong dedicated operator identity or grant")
        if (
            kind == "google_secret_manager_secret_iam_member"
            and _secret_name(value.get("secret_id")) != "operator-database-url"
        ):
            errors.append(f"{address}: operator receives only its controller database secret")
    if seen != OPERATOR_RESOURCES.keys():
        errors.append(
            "Operator prerequisite plan must include its GSA and both exact IAM resources"
        )
    return errors


def _tls_recovery_errors(resources):
    errors = []
    seen = set()
    for resource in resources:
        if resource.get("mode") != "managed":
            continue
        address = resource.get("address")
        change = resource.get("change", {})
        if address not in TLS_RECOVERY_ADDRESSES:
            if change.get("actions") != ["no-op"]:
                errors.append(f"{address}: TLS custody cannot change existing resources")
            continue
        seen.add(address)
        store = address.split('["', 1)[1].split('"', 1)[0]
        value = change.get("after")
        if not isinstance(value, dict):
            value = {}
        if (
            resource.get("type") != "google_secret_manager_secret"
            or value.get("secret_id") != f"ec-dev-{store}-ca-recovery-g1"
            or not _safe_tls_recovery(value)
        ):
            errors.append(f"{address}: wrong TLS recovery container or protection")
    if seen != TLS_RECOVERY_ADDRESSES:
        errors.append("TLS custody plan must include all three exact recovery containers")
    return errors


def inspect(plan, *, operator_prerequisite=False, tls_recovery=False):
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
    if operator_prerequisite:
        errors.extend(_operator_prerequisite_errors(plan.get("resource_changes", [])))
    if tls_recovery:
        errors.extend(_tls_recovery_errors(plan.get("resource_changes", [])))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--operator-prerequisite",
        action="store_true",
        help="Allow only the dedicated operator GSA and its exact two IAM additions; keep existing resources unchanged",
    )
    parser.add_argument(
        "--tls-recovery",
        action="store_true",
        help="Allow only the three protected TLS recovery containers; keep existing resources unchanged",
    )
    parser.add_argument("plan", type=Path, help="Saved terraform show -json application plan")
    args = parser.parse_args(argv)
    errors = inspect(
        json.loads(args.plan.read_text()),
        operator_prerequisite=args.operator_prerequisite,
        tls_recovery=args.tls_recovery,
    )
    print("\n".join(errors) if errors else "Shared-development application plan scope passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
