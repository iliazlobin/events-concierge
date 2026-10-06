"""Shared app landing cannot adopt platform resources or expose the private app."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELM = os.environ.get("HELM", "helm")
spec = importlib.util.spec_from_file_location(
    "shared_plan_policy", ROOT / "scripts/development/check_shared_plan.py"
)
assert spec is not None and spec.loader is not None
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
PROJECT = "iz27-platform-dev"


def _member(name):
    return f"serviceAccount:ec-dev-{name}@{PROJECT}.iam.gserviceaccount.com"


def _plan(kind, value, actions=None):
    return {
        "resource_changes": [
            {
                "address": f"{kind}.fixture",
                "mode": "managed",
                "type": kind,
                "change": {"actions": actions or ["create"], "after": value},
            }
        ]
    }


def _bucket(name="iz27-platform-dev-ec-payloads"):
    return {
        "project": PROJECT,
        "name": name,
        "location": "US-WEST1",
        "force_destroy": False,
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
        "versioning": [{"enabled": name != policy.MEDIA_BUCKET}],
        "soft_delete_policy": [
            {"retention_duration_seconds": 0 if name == policy.MEDIA_BUCKET else 604800}
        ],
    }


@pytest.mark.parametrize(
    "kind",
    [
        "google_project_service",
        "google_project_iam_member",
        "google_container_cluster",
        "google_container_node_pool",
        "google_compute_network",
        "google_compute_firewall",
        "google_compute_router",
        "google_storage_bucket_iam_binding",
        "kubernetes_storage_class",
    ],
)
@pytest.mark.parametrize("actions", [["create"], ["no-op"]])
def test_shared_app_plan_rejects_platform_ownership_even_if_unchanged(kind, actions):
    assert policy.inspect(_plan(kind, {"project": PROJECT}, actions))


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"]])
def test_shared_app_plan_rejects_unreviewed_mutation_or_replacement(actions):
    assert policy.inspect(_plan("google_storage_bucket", _bucket(), actions))


@pytest.mark.parametrize("name", sorted(policy.BUCKETS))
def test_shared_app_plan_admits_only_protected_app_buckets(name):
    assert policy.inspect(_plan("google_storage_bucket", _bucket(name))) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project", "project-9c8cce04-f94d-40fc-aa6"),
        ("name", "iz27-platform-gke-state"),
        ("force_destroy", True),
        ("uniform_bucket_level_access", False),
        ("public_access_prevention", "inherited"),
        ("versioning", [{"enabled": False}]),
        ("soft_delete_policy", [{"retention_duration_seconds": 0}]),
        ("location", "US"),
    ],
)
def test_shared_app_plan_rejects_bucket_boundary_drift(field, value):
    bucket = _bucket()
    bucket[field] = value
    assert policy.inspect(_plan("google_storage_bucket", bucket))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("versioning", [{"enabled": True}]),
        ("soft_delete_policy", [{"retention_duration_seconds": 604800}]),
        ("retention_policy", [{"retention_period": 60}]),
        ("default_event_based_hold", True),
        ("lifecycle_rule", [{"action": [{"type": "Delete"}]}]),
    ],
)
def test_media_policy_rejects_hidden_retention_or_expiration(field, value):
    bucket = _bucket(policy.MEDIA_BUCKET)
    bucket[field] = value
    assert policy.inspect(_plan("google_storage_bucket", bucket))


def test_media_access_is_limited_to_profile_and_erasure_workloads():
    for name in policy.IDENTITY_NAMES | {"outside-app"}:
        for role in (
            "roles/storage.objectUser",
            "roles/storage.legacyBucketReader",
            "roles/storage.admin",
        ):
            errors = policy.inspect(
                _plan(
                    "google_storage_bucket_iam_member",
                    {
                        "bucket": policy.MEDIA_BUCKET,
                        "member": _member(name),
                        "role": role,
                    },
                )
            )
            allowed = name in policy.MEDIA_WORKLOADS and role != "roles/storage.admin"
            assert bool(errors) is not allowed


def test_shared_app_plan_restricts_node_access_to_its_repository():
    value = {
        "project": PROJECT,
        "location": "us-west1",
        "repository": "ec-dev",
        "member": f"serviceAccount:platform-dev-node@{PROJECT}.iam.gserviceaccount.com",
        "role": "roles/artifactregistry.reader",
    }
    assert policy.inspect(_plan("google_artifact_registry_repository_iam_member", value)) == []
    for field, unsafe in [
        ("repository", "another-app"),
        ("role", "roles/artifactregistry.admin"),
        ("member", "allUsers"),
    ]:
        assert policy.inspect(
            _plan("google_artifact_registry_repository_iam_member", {**value, field: unsafe})
        )


def test_shared_app_plan_preserves_workload_identity_and_credential_separation():
    for name, secrets in policy.SECRETS.items():
        for secret in secrets:
            assert (
                policy.inspect(
                    _plan(
                        "google_secret_manager_secret_iam_member",
                        {
                            "project": PROJECT,
                            "secret_id": "ec-dev-" + secret,
                            "role": "roles/secretmanager.secretAccessor",
                            "member": _member(name),
                        },
                    )
                )
                == []
            )
    assert policy.inspect(
        _plan(
            "google_secret_manager_secret_iam_member",
            {
                "project": PROJECT,
                "secret_id": "ec-dev-migration-url",
                "role": "roles/secretmanager.secretAccessor",
                "member": _member("temporal-catalog"),
            },
        )
    )

    identity = {
        "service_account_id": f"projects/{PROJECT}/serviceAccounts/ec-dev-api@{PROJECT}.iam.gserviceaccount.com",
        "role": "roles/iam.workloadIdentityUser",
        "member": f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-api]",
    }
    assert policy.inspect(_plan("google_service_account_iam_member", identity)) == []
    assert policy.inspect(
        _plan(
            "google_service_account_iam_member",
            {
                **identity,
                "member": identity["member"].replace(
                    "events-concierge-api]", "events-concierge-migration]"
                ),
            },
        )
    )


def _operator_plan():
    resources = []
    for address, kind, value in [
        (
            "google_service_account.operator_api",
            "google_service_account",
            {"project": PROJECT, "account_id": "ec-dev-operator-api"},
        ),
        (
            "google_service_account_iam_member.operator_api_workload",
            "google_service_account_iam_member",
            {
                "service_account_id": f"projects/{PROJECT}/serviceAccounts/ec-dev-operator-api@{PROJECT}.iam.gserviceaccount.com",
                "role": "roles/iam.workloadIdentityUser",
                "member": f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-operator-api]",
            },
        ),
        (
            "google_secret_manager_secret_iam_member.operator_api_database",
            "google_secret_manager_secret_iam_member",
            {
                "project": PROJECT,
                "secret_id": "ec-dev-operator-database-url",
                "member": _member("operator-api"),
                "role": "roles/secretmanager.secretAccessor",
            },
        ),
    ]:
        resource = _plan(kind, value)["resource_changes"][0]
        resource["address"] = address
        resources.append(resource)
    return {"resource_changes": resources}


def test_dedicated_operator_addition_preserves_existing_access_sets():
    assert policy.inspect(_operator_plan(), operator_prerequisite=True) == []
    assert len(policy.NAMES) == 15
    assert "operator-api" not in policy.NAMES | policy.CONSUMERS | policy.CATALOG
    assert policy.SECRETS["operator-api"] == {"operator-database-url"}
    assert sum(len(secrets) for name, secrets in policy.SECRETS.items() if name != "operator-api") == 32


@pytest.mark.parametrize("secret", sorted(policy.SECRET_NAMES - {"operator-database-url"}))
def test_operator_cannot_access_any_other_secret(secret):
    plan = _operator_plan()
    plan["resource_changes"][2]["change"]["after"]["secret_id"] = f"ec-dev-{secret}"
    assert policy.inspect(plan)
    assert policy.inspect(plan, operator_prerequisite=True)


@pytest.mark.parametrize("bucket", sorted(policy.BUCKETS))
@pytest.mark.parametrize("role", ["roles/storage.objectUser", "roles/storage.legacyBucketReader"])
def test_operator_cannot_receive_object_or_bucket_access(bucket, role):
    value = {"bucket": bucket, "role": role, "member": _member("operator-api")}
    assert policy.inspect(_plan("google_storage_bucket_iam_member", value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("member", f"serviceAccount:{PROJECT}.svc.id.goog[default/events-concierge-operator-api]"),
        ("member", f"serviceAccount:{PROJECT}.svc.id.goog[events-concierge-dev/events-concierge-api]"),
        ("member", "serviceAccount:other-project.svc.id.goog[events-concierge-dev/events-concierge-operator-api]"),
        ("service_account_id", f"projects/{PROJECT}/serviceAccounts/ec-dev-api@{PROJECT}.iam.gserviceaccount.com"),
        ("role", "roles/iam.serviceAccountTokenCreator"),
        ("condition", [{"expression": "true"}]),
    ],
)
def test_operator_workload_binding_cannot_broaden_or_target_another_identity(field, value):
    plan = _operator_plan()
    plan["resource_changes"][1]["change"]["after"][field] = value
    assert policy.inspect(plan)
    assert policy.inspect(plan, operator_prerequisite=True)


def test_operator_prerequisite_mode_checks_resource_identity_not_just_addresses():
    plan = _operator_plan()
    # A grant to the old admin is valid in the full landing, but is not the new operator grant.
    plan["resource_changes"][2]["change"]["after"]["member"] = _member("development-admin")
    assert policy.inspect(plan) == []
    assert policy.inspect(plan, operator_prerequisite=True)


def test_operator_prerequisite_mode_requires_all_three_resources_and_no_other_additions():
    plan = _operator_plan()
    assert policy.inspect({"resource_changes": plan["resource_changes"][:-1]}, operator_prerequisite=True)
    extra = _plan("google_storage_bucket", _bucket())["resource_changes"][0]
    plan["resource_changes"].append(extra)
    assert policy.inspect(plan) == []
    assert policy.inspect(plan, operator_prerequisite=True)
    extra["change"]["actions"] = ["no-op"]
    assert policy.inspect(plan, operator_prerequisite=True) == []


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"]])
def test_operator_prerequisite_mode_rejects_replacements_updates_and_deletes(index, actions):
    plan = _operator_plan()
    plan["resource_changes"][index]["change"]["actions"] = actions
    assert policy.inspect(plan, operator_prerequisite=True)


def test_operator_prerequisite_mode_handles_completed_or_partial_additions():
    plan = _operator_plan()
    for resource in plan["resource_changes"]:
        resource["change"]["actions"] = ["no-op"]
        assert policy.inspect(plan, operator_prerequisite=True) == []


def test_operator_prerequisite_flag_scopes_the_saved_plan(tmp_path, capsys):
    plan = _operator_plan()
    plan["resource_changes"].extend(_plan("google_storage_bucket", _bucket())["resource_changes"])
    path = tmp_path / "operator-plan.json"
    path.write_text(json.dumps(plan))
    assert policy.main([str(path)]) == 0
    assert "scope passed" in capsys.readouterr().out
    assert policy.main(["--operator-prerequisite", str(path)]) == 1
    assert "cannot change existing resources" in capsys.readouterr().out


def test_shared_app_plan_keeps_catalog_payload_access_in_its_prefix():
    value = {
        "bucket": "iz27-platform-dev-ec-payloads",
        "role": "roles/storage.objectUser",
        "member": _member("temporal-catalog"),
        "condition": [
            {
                "expression": "resource.name.startsWith('projects/_/buckets/iz27-platform-dev-ec-payloads/objects/events-concierge/catalog/v1/')"
            }
        ],
    }
    assert policy.inspect(_plan("google_storage_bucket_iam_member", value)) == []
    assert policy.inspect(_plan("google_storage_bucket_iam_member", {**value, "condition": []}))
    wrong_prefix = copy.deepcopy(value)
    wrong_prefix["condition"][0]["expression"] = "true"
    assert policy.inspect(_plan("google_storage_bucket_iam_member", wrong_prefix))
    assert policy.inspect(
        _plan("google_storage_bucket_iam_member", {**value, "bucket": "iz27-platform-dev-ec-state"})
    )


@pytest.mark.skipif(not shutil.which(HELM), reason="Helm deployment validation tool required")
def test_shared_helm_app_and_stores_consume_external_platform_without_public_routes():
    command = [
        HELM,
        "template",
        "ec",
        str(ROOT / "deploy/helm/events-concierge"),
        "-n",
        "events-concierge-dev",
    ]
    for filename in ("values-development.yaml", "values-shared-development.yaml"):
        command += ["-f", str(ROOT / "deploy/helm/events-concierge" / filename)]
    settings = {
        "global.releasePhase": "application",
        "global.runtimeProviderReady": "true",
        "global.appImage.repository": f"us-west1-docker.pkg.dev/{PROJECT}/ec-dev/app",
        "global.frontendImage.repository": f"us-west1-docker.pkg.dev/{PROJECT}/ec-dev/web",
        "global.appImage.digest": "sha256:" + "1" * 64,
        "global.frontendImage.digest": "sha256:" + "1" * 64,
        **{
            f"serviceAccounts.{name}.gcpServiceAccount": f"ec-dev-{name}@{PROJECT}.iam.gserviceaccount.com"
            for name in policy.NAMES
        },
    }
    for key, value in settings.items():
        command += ["--set", f"{key}={value}"]
    app = subprocess.run(command, check=True, capture_output=True, text=True)
    stores = subprocess.run(
        [
            HELM,
            "template",
            "ec-dev-data",
            str(ROOT / "deploy/helm/events-concierge-dev-data"),
            "-n",
            "events-concierge-dev",
            "-f",
            str(ROOT / "deploy/helm/events-concierge-dev-data/values-shared-development.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = [d for output in (app.stdout, stores.stdout) for d in yaml.safe_load_all(output) if d]
    forbidden = {
        "StorageClass",
        "Gateway",
        "HTTPRoute",
        "Ingress",
        "CronJob",
        "ClusterRole",
        "ClusterRoleBinding",
    }
    assert not ({d["kind"] for d in docs} & forbidden)
    deployed = {
        d["metadata"]["name"]
        for d in yaml.safe_load_all(app.stdout)
        if d and d["kind"] == "Deployment"
    }
    assert deployed == {
        "events-concierge-" + name
        for name in (
            "api",
            "frontend",
            "admin",
            "temporal-catalog",
            "ingestion-executor",
            "account-erasure",
        )
    }
    claims = [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 3
    assert all(d["spec"]["storageClassName"] == "shared-retain" for d in claims)
    for document in docs:
        if document["kind"] == "Service":
            assert document["spec"].get("type", "ClusterIP") == "ClusterIP"
        if document["kind"] == "SecretProviderClass":
            secrets = yaml.safe_load(document["spec"]["parameters"]["secrets"])
            assert all(f"projects/{PROJECT}/secrets/" in item["resourceName"] for item in secrets)
    config = next(
        d["data"] for d in docs if d["kind"] == "ConfigMap" and "EC_GCP_PROJECT" in d["data"]
    )
    assert config["EC_MEDIA_BACKEND"] == "gcs"
    assert config["EC_GCS_MEDIA_BUCKET"] == policy.MEDIA_BUCKET
    assert config["EC_GCS_MEDIA_PREFIX"] == "events-concierge/media/v1"
    assert config["EC_GCP_PROJECT"] == PROJECT
    assert config["EC_GCS_CLAIM_CHECK_BUCKET"] == "iz27-platform-dev-ec-payloads"
    assert config["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"] == "false"
