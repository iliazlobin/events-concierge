"""Only the reviewed old catalog IAM grants may be revoked by the opt-in cutover gate."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "development_plan", ROOT / "scripts/development/check_plan.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

PROJECT = "project-9c8cce04-f94d-40fc-aa6"
MEMBER = f"serviceAccount:ec-dev-temporal-catalog@{PROJECT}.iam.gserviceaccount.com"


def revocations():
    return [
        {
            "address": 'google_secret_manager_secret_iam_member.reader["temporal-catalog/database-url"]',
            "mode": "managed",
            "type": "google_secret_manager_secret_iam_member",
            "change": {
                "actions": ["delete"],
                "before": {
                    "project": PROJECT,
                    "secret_id": f"projects/{PROJECT}/secrets/ec-dev-database-url",
                    "member": MEMBER,
                    "role": "roles/secretmanager.secretAccessor",
                    "condition": [],
                },
                "after": None,
            },
        },
        {
            "address": 'google_storage_bucket_iam_member.payloads["temporal-catalog"]',
            "mode": "managed",
            "type": "google_storage_bucket_iam_member",
            "change": {
                "actions": ["delete"],
                "before": {
                    "bucket": "b/iz27-ec-dev-payloads",
                    "member": MEMBER,
                    "role": "roles/storage.objectUser",
                    "condition": [],
                },
                "after": None,
            },
        },
    ]


def inspect(resources, *, operator_cutover=True):
    return gate.inspect({"resource_changes": resources}, operator_cutover=operator_cutover)


def test_exact_legacy_revocations_require_explicit_opt_in():
    resources = revocations()
    assert inspect(resources, operator_cutover=False)
    assert inspect(resources) == []
    # Incremental recovery can have only the not-yet-revoked grant remaining.
    for resource in resources:
        assert inspect([resource]) == []


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [
        (0, "project", "other-project"),
        (0, "secret_id", "ec-dev-database-url"),
        (0, "secret_id", "ec-dev-migration-url"),
        (0, "secret_id", f"projects/{PROJECT}/secrets/ec-dev-migration-url"),
        (0, "secret_id", "projects/other-project/secrets/ec-dev-database-url"),
        (0, "secret_id", f"/projects/{PROJECT}/secrets/ec-dev-database-url"),
        (0, "secret_id", f"projects/{PROJECT}/secrets/ec-dev-database-url/versions/latest"),
        (0, "member", f"serviceAccount:ec-dev-api@{PROJECT}.iam.gserviceaccount.com"),
        (0, "role", "roles/owner"),
        (0, "condition", [{"title": "other", "expression": "true"}]),
        (1, "bucket", "iz27-ec-dev-backups"),
        (1, "bucket", "iz27-ec-dev-payloads"),
        (1, "bucket", "b/iz27-ec-dev-backups"),
        (1, "bucket", "gs://iz27-ec-dev-payloads"),
        (1, "bucket", "/b/iz27-ec-dev-payloads"),
        (1, "bucket", "b/iz27-ec-dev-payloads/"),
        (1, "project", "other-project"),
        (1, "member", "allUsers"),
        (
            1,
            "member",
            "serviceAccount:ec-dev-temporal-catalog@other-project.iam.gserviceaccount.com",
        ),
        (1, "role", "roles/storage.admin"),
        (1, "condition", [{"title": "different", "expression": "true"}]),
    ],
)
def test_before_identity_must_match_every_reviewed_field(index, key, value):
    resources = revocations()
    resources[index]["change"]["before"][key] = value
    assert inspect(resources)


@pytest.mark.parametrize("index", [0, 1])
def test_missing_before_identity_fields_fail_closed(index):
    resource = revocations()[index]
    for key in ("member", "role", "project" if index == 0 else "bucket"):
        changed = deepcopy(resource)
        del changed["change"]["before"][key]
        assert inspect([changed])
    resource["change"]["before"] = None
    assert inspect([resource])


@pytest.mark.parametrize("actions", [["update"], ["delete", "create"], ["create", "delete"]])
def test_cutover_does_not_authorize_replacements_or_updates(actions):
    resources = revocations()
    for resource in resources:
        resource["change"]["actions"] = actions
        resource["change"]["after"] = deepcopy(resource["change"]["before"])
    assert inspect(resources)


@pytest.mark.parametrize("index", [0, 1])
def test_delete_requires_an_explicit_null_after_state(index):
    resource = revocations()[index]
    resource["change"]["after"] = {}
    assert inspect([resource])
    del resource["change"]["after"]
    assert inspect([resource])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("address", 'google_secret_manager_secret_iam_member.reader["api/database-url"]'),
        (
            "address",
            'module.other.google_secret_manager_secret_iam_member.reader["temporal-catalog/database-url"]',
        ),
        ("type", "google_project_iam_member"),
        ("mode", "other"),
    ],
)
def test_matching_attributes_cannot_authorize_an_unrelated_resource(key, value):
    resource = revocations()[0]
    resource[key] = value
    assert inspect([resource])


def test_unrelated_delete_is_rejected_even_beside_valid_revocations():
    extra = {
        "address": 'google_service_account.workload["api"]',
        "mode": "managed",
        "type": "google_service_account",
        "change": {"actions": ["delete"], "before": {"project": PROJECT}, "after": None},
    }
    assert inspect([*revocations(), extra])


@pytest.mark.parametrize(
    "after",
    [
        {"project": "other-project", "member": MEMBER, "role": "roles/viewer"},
        {"project": PROJECT, "member": "allUsers", "role": "roles/viewer"},
        {"project": PROJECT, "member": MEMBER, "role": "roles/editor"},
    ],
)
def test_cutover_mode_retains_existing_creation_security_checks(after):
    create = {
        "address": "google_project_iam_member.extra",
        "mode": "managed",
        "type": "google_project_iam_member",
        "change": {"actions": ["create"], "before": None, "after": after},
    }
    assert inspect([*revocations(), create])


def test_safe_creation_stays_allowed_in_both_modes():
    create = {
        "address": 'google_secret_manager_secret.development["operator-database-url"]',
        "mode": "managed",
        "type": "google_secret_manager_secret",
        "change": {
            "actions": ["create"],
            "before": None,
            "after": {"project": PROJECT, "secret_id": "ec-dev-operator-database-url"},
        },
    }
    assert inspect([create], operator_cutover=False) == []
    assert inspect([*revocations(), create]) == []


def test_cli_flag_is_required_for_the_same_saved_plan(tmp_path, capsys):
    plan = tmp_path / "cutover.json"
    plan.write_text(json.dumps({"resource_changes": revocations()}))
    assert gate.main([str(plan)]) == 1
    assert "only initial additions allowed" in capsys.readouterr().out
    assert gate.main(["--operator-cutover", str(plan)]) == 0
    assert "Development plan scope passed" in capsys.readouterr().out
