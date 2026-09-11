"""Cutover recovery contracts; no cluster, cloud, or live database calls."""

import importlib.util
import json
import subprocess
import uuid
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
        "metadata": {
            "name": name,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_DNS, name)),
            "resourceVersion": "123",
        },
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
        if args[:2] == ("get", "cronjobs,jobs"):
            return SimpleNamespace(stdout=b'{"items": []}')
        if args[0] == "get" and args[1].startswith("deployment/"):
            name = args[1].split("/", 1)[1]
            return SimpleNamespace(
                stdout=json.dumps(next(d for d in deployments if d["metadata"]["name"] == name))
            )
        if "pg_dump" in args:
            kwargs["stdout"].write(b"isolated fake database backup")
        if args[-1] == backup.SNAPSHOT_SQL:
            return SimpleNamespace(stdout=b'{"tenants":2,"requests":2,"schema":"0193"}')
        if args[-1] == "select version_num from alembic_version":
            return SimpleNamespace(stdout=b"0193\n")
        return SimpleNamespace(stdout=b"")

    def cloud(*args, **kwargs):
        events.append(("cloud", *args))
        if args[0] == "cp":
            copies[Path(args[1]).name] = Path(args[1]).read_bytes()
        if args[0] == "cat":
            return SimpleNamespace(stdout=copies[Path(args[1]).name])

    with (
        patch.object(backup.subprocess, "check_output", return_value=backup.CONTEXT),
        patch.object(backup, "k", side_effect=kubectl),
        patch.object(backup, "gc", side_effect=cloud),
    ):
        yield events, copies


@pytest.mark.parametrize(
    "resource",
    [
        {"kind": "CronJob", "metadata": {"name": "cadence"}, "spec": {"suspend": False}},
        {"kind": "Job", "metadata": {"name": "pending"}, "status": {}},
        {"kind": "Job", "metadata": {"name": "retrying"}, "status": {"failed": 1}},
    ],
)
def test_backup_refuses_active_or_pending_scheduled_writers_before_any_mutation(resource):
    with (
        patch.object(backup, "_check_context"),
        patch.object(
            backup, "k", return_value=SimpleNamespace(stdout=json.dumps({"items": [resource]}))
        ) as cluster,
        patch.object(backup, "gc") as cloud,
        pytest.raises(SystemExit, match="before backup"),
    ):
        backup.backup()
    assert cluster.call_count == 1
    cloud.assert_not_called()


def test_backup_accepts_suspended_cadence_and_terminal_jobs():
    resources = [
        {"kind": "CronJob", "metadata": {"name": "cadence"}, "spec": {"suspend": True}},
        {
            "kind": "Job",
            "metadata": {"name": "done"},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        },
        {
            "kind": "Job",
            "metadata": {"name": "failed"},
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
    ]
    with patch.object(
        backup, "k", return_value=SimpleNamespace(stdout=json.dumps({"items": resources}))
    ):
        backup._check_scheduled_writers_quiet()


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
    recovery = json.loads(copies["recovery.json"])
    backup._validate_recovery(recovery, backup.BACKUP_ROOT + recovery["id"])
    assert recovery["replicas"] == manifest["quiesced_deployments"]
    saved = next(i for i, event in enumerate(events) if event[:2] == ("cloud", "cp"))
    first_scale = next(i for i, event in enumerate(events) if event[0] == "scale")
    assert saved < first_scale


def test_regular_backup_restores_original_replica_counts(recording):
    events, copies = recording
    backup.backup()
    assert [event[:4] for event in events if event[0] == "scale"][-3:] == [
        ("scale", "deployment/ec-dev-temporal-frontend", "--replicas=1", "--request-timeout=20s"),
        ("scale", "deployment/events-concierge-api", "--replicas=1", "--request-timeout=20s"),
        (
            "scale",
            "deployment/events-concierge-ingestion-executor",
            "--replicas=0",
            "--request-timeout=20s",
        ),
    ]
    assert json.loads(copies["manifest.json"])["resume_required"] is False


def test_failed_recovery_upload_does_not_stop_any_workload(recording):
    events, copies = recording
    with (
        patch.object(backup, "gc", side_effect=subprocess.CalledProcessError(1, ["upload"])),
        pytest.raises(subprocess.CalledProcessError),
    ):
        backup.backup(hold_stopped=True)
    assert not any(event[0] == "scale" for event in events)
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


def test_recovery_readback_failure_never_stops_workloads(recording):
    events, copies = recording
    original_gc = backup.gc.side_effect

    def cloud(*args, **kwargs):
        if args[0] == "cat":
            raise subprocess.CalledProcessError(1, args, stderr=b"no such host")
        return original_gc(*args, **kwargs)

    backup.gc.side_effect = cloud
    with pytest.raises(subprocess.CalledProcessError):
        backup.backup(hold_stopped=True)
    assert "recovery.json" in copies
    assert not any(event[0] == "scale" for event in events)


def test_recovery_readback_mismatch_never_stops_workloads(recording):
    events, _copies = recording
    original_gc = backup.gc.side_effect

    def cloud(*args, **kwargs):
        result = original_gc(*args, **kwargs)
        if args[0] == "cat":
            data = json.loads(result.stdout)
            data["replicas"]["events-concierge-api"] = 0
            return SimpleNamespace(stdout=json.dumps(data))
        return result

    backup.gc.side_effect = cloud
    with pytest.raises(SystemExit, match="read-back mismatch"):
        backup.backup(hold_stopped=True)
    assert not any(event[0] == "scale" for event in events)


def test_resume_refuses_changed_schema_before_any_scale(recovery_record):
    api = deployment("events-concierge-api", 0)
    with (
        patch.object(backup, "_check_context"),
        patch.object(backup, "_read_schema", return_value="0193"),
        patch.object(
            backup, "gc", return_value=SimpleNamespace(stdout=json.dumps(recovery_record))
        ),
        patch.object(
            backup, "k", return_value=SimpleNamespace(stdout=json.dumps({"items": [api]}))
        ) as k,
        pytest.raises(RuntimeError, match="schema changed"),
    ):
        backup.resume(RECOVERY_URI)
    assert all(call.args[0] == "get" for call in k.call_args_list)


def test_recovery_schema_check_retries_transient_failures(recovery_record):
    error = subprocess.CalledProcessError(1, ["read"], stderr=b"no such host")
    with (
        patch.object(backup, "_read_schema", side_effect=[error, "0180"]) as read,
        patch.object(backup.time, "sleep") as sleep,
    ):
        backup._check_recovery_schema(recovery_record)
    assert read.call_count == 2
    sleep.assert_called_once_with(2)


def test_automatic_resume_uses_saved_deployment_and_schema_guards(recording):
    with patch.object(backup, "_resume") as resume:
        backup.backup()
    metadata = resume.call_args.kwargs["recovery"]
    assert metadata["schema"] == "0193"
    assert resume.call_args.args[0] == metadata["replicas"]
    assert (
        metadata["deployment_uids"]["events-concierge-api"]
        == deployment("events-concierge-api")["metadata"]["uid"]
    )


def test_failed_dump_upload_restores_writers_even_with_hold_stopped(recording):
    events, copies = recording
    original_gc = backup.gc.side_effect

    def upload(*args, **kwargs):
        if args[-1].endswith(".dump"):
            raise subprocess.CalledProcessError(1, ["upload"])
        return original_gc(*args, **kwargs)

    backup.gc.side_effect = upload
    with pytest.raises(subprocess.CalledProcessError):
        backup.backup(hold_stopped=True)
    assert (
        "scale",
        "deployment/events-concierge-api",
        "--replicas=1",
        "--request-timeout=20s",
    ) in [event[:4] for event in events]
    assert (
        "scale",
        "deployment/ec-dev-temporal-frontend",
        "--replicas=1",
        "--request-timeout=20s",
    ) in [event[:4] for event in events]
    assert "recovery.json" in copies
    assert "COMPLETE" not in copies


def test_backup_failure_and_recovery_failure_are_both_retained(recording):
    original_gc = backup.gc.side_effect
    original_error = subprocess.CalledProcessError(1, ["upload"])
    recovery_error = RuntimeError("cluster unavailable")

    def upload(*args, **kwargs):
        if args[-1].endswith(".dump"):
            raise original_error
        return original_gc(*args, **kwargs)

    backup.gc.side_effect = upload
    with (
        patch.object(backup, "_resume", side_effect=recovery_error),
        pytest.raises(ExceptionGroup) as caught,
    ):
        backup.backup(hold_stopped=True)
    assert caught.value.exceptions == (original_error, recovery_error)


def test_failed_streaming_dump_is_never_retried(recording):
    events, copies = recording
    original_k = backup.k.side_effect

    def interrupted_dump(*args, **kwargs):
        if "pg_dump" in args:
            events.append(args)
            kwargs["stdout"].write(b"partial dump")
            raise subprocess.CalledProcessError(1, args, stderr=b"no such host")
        return original_k(*args, **kwargs)

    backup.k.side_effect = interrupted_dump
    with pytest.raises(subprocess.CalledProcessError):
        backup.backup()
    assert sum("pg_dump" in event for event in events) == 1
    assert not any(name.endswith(".dump") for name in copies)


@pytest.mark.parametrize("message", [b"no such host", b"ServiceUnavailable", b"TooManyRequests"])
def test_resume_retries_transient_errors_and_bounds_each_call(message):
    error = subprocess.CalledProcessError(1, ["scale"], stderr=message)
    with (
        patch.object(backup, "k", side_effect=[error, None]) as k,
        patch.object(backup.time, "sleep") as sleep,
    ):
        backup._resume({"events-concierge-api": 1})
    assert k.call_count == 2
    assert k.call_args.kwargs["timeout"] == 30
    assert "--request-timeout=20s" in k.call_args.args
    sleep.assert_called_once_with(2)


def test_resume_retry_exhaustion_still_attempts_other_deployments():
    error = subprocess.CalledProcessError(1, ["scale"], stderr=b"no such host")
    attempts = len(backup.RESUME_RETRY_DELAYS) + 1
    with (
        patch.object(backup, "k", side_effect=[error] * attempts + [None]) as k,
        patch.object(backup.time, "sleep") as sleep,
        pytest.raises(ExceptionGroup),
    ):
        backup._resume({"ec-dev-temporal-frontend": 1, "events-concierge-api": 1})
    assert k.call_count == attempts + 1
    assert sleep.call_count == attempts - 1


def test_resume_does_not_retry_forbidden():
    error = subprocess.CalledProcessError(1, ["scale"], stderr=b"Error from server (Forbidden)")
    with (
        patch.object(backup, "k", side_effect=error) as k,
        patch.object(backup.time, "sleep") as sleep,
        pytest.raises(ExceptionGroup),
    ):
        backup._resume({"events-concierge-api": 1})
    assert k.call_count == 1
    sleep.assert_not_called()


RECOVERY_ID = "20260910T230000Z-1234abcd"
RECOVERY_URI = backup.BACKUP_ROOT + RECOVERY_ID


@pytest.fixture
def recovery_record():
    return backup._recovery_metadata(RECOVERY_ID, [deployment("events-concierge-api")], "0180")


@pytest.mark.parametrize(
    "field,value",
    [
        ("context", "other-cluster"),
        ("project", "other-project"),
        ("namespace", "production"),
        ("id", "20260909T230000Z-1234abcd"),
        ("version", True),
        ("replicas", {"ec-dev-redis": 1}),
        ("replicas", {"arbitrary-deployment": 1}),
        ("replicas", {"events-concierge-api": True}),
        ("replicas", {"events-concierge-api": 2}),
        ("replicas", {"events-concierge-api": -1}),
        ("replicas", {}),
        ("deployment_uids", {"events-concierge-api": "invalid"}),
        ("template_sha256", {"events-concierge-api": "invalid"}),
    ],
)
def test_invalid_recovery_is_rejected_without_cluster_writes(recovery_record, field, value):
    recovery_record[field] = value
    with (
        patch.object(backup, "_check_context"),
        patch.object(
            backup, "gc", return_value=SimpleNamespace(stdout=json.dumps(recovery_record))
        ),
        patch.object(backup, "k") as k,
        pytest.raises(SystemExit),
    ):
        backup.resume(RECOVERY_URI)
    k.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "gs://other-bucket/20260910T230000Z-1234abcd",
        RECOVERY_URI + "/",
        RECOVERY_URI + "/../other",
        backup.BACKUP_ROOT + "20261310T230000Z-1234abcd",
    ],
)
def test_resume_rejects_invalid_prefix_without_cloud_calls(uri):
    with (
        patch.object(backup, "gc") as gc,
        patch.object(backup, "k") as k,
        pytest.raises(SystemExit),
    ):
        backup.resume(uri)
    gc.assert_not_called()
    k.assert_not_called()


def test_explicit_resume_works_without_complete_and_is_idempotent(recovery_record):
    api = deployment("events-concierge-api", 0)

    def cluster(*args, **kwargs):
        if args[:2] == ("get", "deployments"):
            return SimpleNamespace(stdout=json.dumps({"items": [api]}))
        if args[0] == "get":
            return SimpleNamespace(stdout=json.dumps(api))
        assert "--resource-version=" + api["metadata"]["resourceVersion"] in args
        api["spec"]["replicas"] = 1
        api["metadata"]["resourceVersion"] = str(int(api["metadata"]["resourceVersion"]) + 1)

    with (
        patch.object(backup, "_check_context"),
        patch.object(backup, "_read_schema", return_value="0180"),
        patch.object(
            backup, "gc", return_value=SimpleNamespace(stdout=json.dumps(recovery_record))
        ) as gc,
        patch.object(backup, "k", side_effect=cluster) as k,
    ):
        backup.resume(RECOVERY_URI)
        backup.resume(RECOVERY_URI)
    assert api["spec"]["replicas"] == 1
    assert sum(call.args[0] == "scale" for call in k.call_args_list) == 2
    assert all(call.args == ("cat", RECOVERY_URI + "/recovery.json") for call in gc.call_args_list)


@pytest.mark.parametrize("change", ["missing", "replaced", "changed_template"])
def test_resume_refuses_changed_workload_before_any_scale(recovery_record, change):
    api = deployment("events-concierge-api", 0)
    if change == "replaced":
        api["metadata"]["uid"] = str(uuid.uuid4())
    if change == "changed_template":
        api["spec"]["template"]["spec"]["containers"][0]["image"] = "new-image"
    items = [] if change == "missing" else [api]
    with (
        patch.object(backup, "_check_context"),
        patch.object(
            backup, "gc", return_value=SimpleNamespace(stdout=json.dumps(recovery_record))
        ),
        patch.object(
            backup, "k", return_value=SimpleNamespace(stdout=json.dumps({"items": items}))
        ) as k,
        pytest.raises((SystemExit, RuntimeError)),
    ):
        backup.resume(RECOVERY_URI)
    assert all(call.args[0] == "get" for call in k.call_args_list)


def test_resume_revalidates_identity_after_transient_scale_failure(recovery_record):
    api = deployment("events-concierge-api", 0)
    replaced = deployment("events-concierge-api", 0)
    replaced["metadata"]["uid"] = str(uuid.uuid4())
    with (
        patch.object(backup, "_read_schema", return_value="0180"),
        patch.object(
            backup,
            "k",
            side_effect=[
                SimpleNamespace(stdout=json.dumps(api)),
                subprocess.CalledProcessError(1, ["scale"], stderr=b"no such host"),
                SimpleNamespace(stdout=json.dumps(replaced)),
            ],
        ) as k,
        patch.object(backup.time, "sleep"),
        pytest.raises(ExceptionGroup),
    ):
        backup._resume(recovery_record["replicas"], recovery=recovery_record)
    assert sum(call.args[0] == "scale" for call in k.call_args_list) == 1


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


def test_restore_waits_for_final_tcp_server_before_bootstrap():
    dump = b"isolated temporal dump"
    name = "temporal-temporal.dump"
    manifest = {
        "schema": "0193",
        "databases": {
            name: {
                "store": "temporal",
                "database": "temporal",
                "sha256": backup.hashlib.sha256(dump).hexdigest(),
            }
        },
    }
    tcp_checks = 0
    commands = []
    verified = []

    def cloud(*args, **kwargs):
        if args[0] == "cp" and args[1].startswith("gs://"):
            filename = Path(args[1]).name
            content = {
                "COMPLETE": b"complete",
                "manifest.json": json.dumps(manifest).encode(),
                name: dump,
            }[filename]
            (Path(args[2]) / filename).write_bytes(content)
        elif args[0] == "rsync":
            Path(args[-1]).mkdir()
        elif args[0] == "cp":
            verified.append(Path(args[1]).name)

    def docker(args, **kwargs):
        nonlocal tcp_checks
        commands.append(args)
        if "pg_isready" in args:
            # During initialization, a socket probe succeeds prematurely.
            if "-h" not in args or args[args.index("-h") + 1] != "127.0.0.1":
                return SimpleNamespace(returncode=0)
            tcp_checks += 1
            return SimpleNamespace(returncode=1 if tcp_checks == 1 else 0)
        if "psql" in args or "pg_restore" in args:
            assert tcp_checks >= 2, "Restore began against the temporary initialization server"
        return SimpleNamespace(returncode=0, stdout=b"")

    with (
        patch.object(backup, "gc", side_effect=cloud),
        patch.object(backup.subprocess, "run", side_effect=docker),
        patch.object(backup.time, "sleep") as sleep,
    ):
        backup.verify(RECOVERY_URI)
    assert tcp_checks == 2
    sleep.assert_called_once_with(1)
    assert any("pg_restore" in args for args in commands)
    assert any(args[:3] == ["docker", "rm", "-fv"] for args in commands)
    assert verified == ["VERIFIED.json"]


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
