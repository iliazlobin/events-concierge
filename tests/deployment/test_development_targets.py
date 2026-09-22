"""Retired destinations must fail before secret, backup or release operations."""

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from events_concierge.deployment.development_targets import TARGETS

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts/development" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_bootstrap_refuses_legacy_context_before_any_secret_call():
    module = load("bootstrap_secrets")
    with (
        patch.object(
            module.subprocess,
            "check_output",
            return_value="gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev",
        ),
        patch.object(module, "gc") as cloud,
        pytest.raises(SystemExit, match="cluster context"),
    ):
        module.initialize_credentials(project_to_kubernetes=True)
    cloud.assert_not_called()


def test_shared_backup_refuses_legacy_uri_and_context_before_cloud_io():
    module = load("backup")
    uri = "gs://iz27-ec-dev-backups/20260911T053103Z-e6678337"
    with patch.object(module, "gc") as cloud:
        with pytest.raises(SystemExit, match="backup prefix"):
            module.resume(uri)
        with pytest.raises(SystemExit, match="backup prefix"):
            module.verify(uri)
        with (
            patch.object(
                module.subprocess,
                "check_output",
                return_value="gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev",
            ),
            pytest.raises(SystemExit, match="Wrong cluster"),
        ):
            module.backup()
    cloud.assert_not_called()


def test_shared_commands_keep_selected_project_context_and_payload_bucket():
    module = load("backup")
    with patch.object(module, "run") as command:
        module.k("get", "pods")
        assert "--context=" + TARGETS["shared"].context in command.call_args.args[0]
        module.gc("ls", module.BACKUP_ROOT)
        assert "--project=iz27-platform-dev" in command.call_args.args[0]
        assert "--account=iliazlobin27@gmail.com" in command.call_args.args[0]
    assert module.PAYLOAD_ROOT == "gs://iz27-platform-dev-ec-payloads"
    assert module.TARGET_NAME == "shared"


@pytest.mark.parametrize("script", ["wait_ready", "check_store_recovery"])
def test_shared_acceptance_helpers_refuse_legacy_context_before_inspection_or_writes(script):
    module = load(script)
    with (
        patch.object(
            module.subprocess,
            "check_output",
            return_value="gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev",
        ) as command,
        patch.object(module.subprocess, "run") as mutation,
        pytest.raises(SystemExit, match="Wrong cluster"),
    ):
        module.main()
    assert command.call_count == 1
    mutation.assert_not_called()


@pytest.mark.parametrize("wrong_field", [None, "service_accounts", "cluster", "namespace"])
def test_release_values_use_selected_state_and_refuse_cross_target_outputs(tmp_path, wrong_field):
    out = {
        "service_accounts": {
            "value": {"api": "ec-dev-api@iz27-platform-dev.iam.gserviceaccount.com"}
        },
        "cluster": {"value": "platform-dev"},
        "namespace": {"value": "events-concierge-dev"},
    }
    if wrong_field:
        out[wrong_field]["value"] = (
            {"api": "ec-dev-api@wrong.iam.gserviceaccount.com"}
            if wrong_field == "service_accounts"
            else "wrong"
        )
    output = tmp_path / "release.yaml"
    image = "us-west1-docker.pkg.dev/iz27-platform-dev/ec-dev/app@sha256:" + "a" * 64
    args = [
        "release_values.py",
        "--app-image",
        image,
        "--web-image",
        image,
        "--revision",
        "b" * 40,
        "--output",
        str(output),
    ]
    with (
        patch.object(sys, "argv", args),
        patch("subprocess.check_output", return_value=json.dumps(out).encode()) as command,
    ):
        if wrong_field:
            with pytest.raises(SystemExit, match="selected destination"):
                runpy.run_path(str(ROOT / "scripts/development/release_values.py"))
            assert not output.exists()
        else:
            runpy.run_path(str(ROOT / "scripts/development/release_values.py"))
            assert "ec-dev-api@iz27-platform-dev" in output.read_text()
    assert "shared-development" in command.call_args.args[0][1]


@pytest.mark.parametrize(
    "script,arguments",
    [
        ("backup", ["backup"]),
        ("bootstrap_secrets", []),
        ("check_store_recovery", []),
        ("wait_ready", []),
        (
            "release_values",
            [
                "--app-image",
                "unused",
                "--web-image",
                "unused",
                "--revision",
                "unused",
                "--output",
                "unused",
            ],
        ),
    ],
)
def test_retired_target_is_rejected_before_any_subprocess(script, arguments):
    with (
        patch.object(sys, "argv", [script + ".py", *arguments, "--target", "legacy"]),
        patch("subprocess.run") as run,
        patch("subprocess.check_output") as output,
        pytest.raises(SystemExit) as error,
    ):
        runpy.run_path(str(ROOT / "scripts/development" / (script + ".py")), run_name="__main__")
    assert error.value.code == 2
    run.assert_not_called()
    output.assert_not_called()
