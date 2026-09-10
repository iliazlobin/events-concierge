"""Cutover recovery contracts; no cluster, cloud, or live database calls."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "development_backup", Path(__file__).resolve().parents[2] / "scripts/development/backup.py"
)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def deployment(name, replicas=1):
    return {
        "metadata": {"name": name},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"component": name}},
            "template": {"spec": {"containers": [{"image": "example@sha256:123"}]}},
        },
    }


@pytest.fixture
def recording():
    events = []
    copies = {}
    deployments = [
        deployment("ec-dev-redis"),
        deployment("ec-dev-temporal-frontend"),
        deployment("events-concierge-api"),
        deployment("events-concierge-ingestion-executor", 0),
    ]

    def kubectl(*args, **kwargs):
        events.append(args)
        if args[:2] == ("get", "deployments"):
            return SimpleNamespace(stdout=json.dumps({"items": deployments}).encode())
        if "pg_dump" in args:
            kwargs["stdout"].write(b"isolated fake database backup")
        if args[-1] == backup.SNAPSHOT_SQL:
            return SimpleNamespace(stdout=b'{"tenants":2,"requests":2,"schema":"0193"}')
        if args[-1] == "select version_num from alembic_version":
            return SimpleNamespace(stdout=b"0193\n")
        return SimpleNamespace(stdout=b"")

    def cloud(*args, **kwargs):
        if args[0] == "cp":
            copies[Path(args[1]).name] = Path(args[1]).read_bytes()

    with (
        patch.object(backup.subprocess, "check_output", return_value=backup.CONTEXT),
        patch.object(backup, "k", side_effect=kubectl),
        patch.object(backup, "gc", side_effect=cloud),
    ):
        yield events, copies


def test_hold_stopped_keeps_writers_down_without_restarting_redis(recording):
    events, copies = recording
    backup.backup(hold_stopped=True)
    scales = [event for event in events if event[0] == "scale"]
    assert scales and all(event[2] == "--replicas=0" for event in scales)
    assert not any("ec-dev-redis" in " ".join(event) for event in events[1:])
    app_stopped = events.index(
        ("wait", "--for=delete", "pod", "-l", "component=events-concierge-api", "--timeout=180s")
    )
    temporal_stop = events.index(("scale", "deployment/ec-dev-temporal-frontend", "--replicas=0"))
    assert app_stopped < temporal_stop
    manifest = json.loads(copies["manifest.json"])
    assert manifest["resume_required"] is True
    assert manifest["replicas"]["ec-dev-redis"] == 1
    assert "ec-dev-redis" not in manifest["quiesced_deployments"]
    assert manifest["schema"] == "0193"
    assert "COMPLETE" in copies


def test_regular_backup_restores_original_replica_counts(recording):
    events, copies = recording
    backup.backup()
    assert events[-3:] == [
        ("scale", "deployment/ec-dev-temporal-frontend", "--replicas=1"),
        ("scale", "deployment/events-concierge-api", "--replicas=1"),
        ("scale", "deployment/events-concierge-ingestion-executor", "--replicas=0"),
    ]
    assert json.loads(copies["manifest.json"])["resume_required"] is False


def test_failed_upload_restores_writers_even_with_hold_stopped(recording):
    events, copies = recording
    with (
        patch.object(backup, "gc", side_effect=subprocess.CalledProcessError(1, ["upload"])),
        pytest.raises(subprocess.CalledProcessError),
    ):
        backup.backup(hold_stopped=True)
    assert ("scale", "deployment/events-concierge-api", "--replicas=1") in events
    assert ("scale", "deployment/ec-dev-temporal-frontend", "--replicas=1") in events
    assert "COMPLETE" not in copies


def test_resume_attempts_all_deployments_after_one_failure():
    with (
        patch.object(
            backup, "k", side_effect=[subprocess.CalledProcessError(1, ["scale"]), None]
        ) as k,
        pytest.raises(ExceptionGroup),
    ):
        backup._resume({"ec-dev-temporal-frontend": 1, "events-concierge-api": 1})
    assert k.call_count == 2


def test_restored_operator_identities_are_separate_from_consumer():
    old = backup.restore_role_sql("0180")
    current = backup.restore_role_sql("0193")
    assert "ec_operator" not in old
    for name in (
        "ec_dev_operator",
        "ec_dev_ingestion",
        "ec_operator_viewer",
        "ec_operator_controller",
        "ec_ingestion_executor",
        "ec_operator_aggregate_definer",
    ):
        assert "CREATE ROLE " + name in current
    assert "GRANT ec_operator_controller TO ec_dev_operator;" in current
    assert "GRANT ec_ingestion_executor TO ec_dev_ingestion;" in current
    assert " TO ec_app" not in current
    assert "ec_operator_aggregate_definer NOLOGIN NOINHERIT NOSUPERUSER" in current
    assert "CREATE ROLE ec_app LOGIN NOSUPERUSER" in current


def test_kubectl_calls_always_pin_development_context():
    with patch.object(backup, "run") as run:
        backup.k("get", "pods")
    assert run.call_args.args[0] == [
        backup.K,
        "--context=" + backup.CONTEXT,
        "-n",
        backup.NS,
        "get",
        "pods",
    ]
