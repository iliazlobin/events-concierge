"""Cutover recovery contracts; no cluster, cloud, or live database calls."""

import importlib.util
import json
import runpy
import subprocess
import sys
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


def test_object_snapshot_verifies_avatar_bytes_and_detects_missing_extra_or_modified_files(
    tmp_path,
):
    media = tmp_path / "media.webp"
    media.write_bytes(b"reviewed avatar")
    inventory = backup._object_inventory(tmp_path)
    assert backup._verify_object_inventory(tmp_path, inventory)
    media.write_bytes(b"changed avatar")
    with pytest.raises(SystemExit, match="checksum mismatch"):
        backup._verify_object_inventory(tmp_path, inventory)
    media.unlink()
    with pytest.raises(SystemExit, match="checksum mismatch"):
        backup._verify_object_inventory(tmp_path, inventory)
    media.write_bytes(b"reviewed avatar")
    (tmp_path / "extra.webp").write_bytes(b"extra")
    with pytest.raises(SystemExit, match="checksum mismatch"):
        backup._verify_object_inventory(tmp_path, inventory)


def test_old_payload_only_backups_remain_supported_without_claiming_media_verification(tmp_path):
    (tmp_path / "claim.payload").write_bytes(b"original")
    assert backup._verify_object_inventory(tmp_path, None) is False
    (tmp_path / "avatar.webp").write_bytes(b"unverified")
    with pytest.raises(SystemExit, match="cannot verify"):
        backup._verify_object_inventory(tmp_path, None)


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


@pytest.mark.parametrize("schema", ["0180", "0193", "0200"])
def test_old_schema_restore_does_not_require_model_usage_definer(schema):
    assert "ec_model_usage_definer" not in backup.restore_role_sql(schema)
    with patch.object(backup, "run") as sql:
        backup.verify_model_usage_role(sql, schema)
    sql.assert_not_called()


@pytest.mark.parametrize("schema", ["0201", "0205"])
def test_model_usage_definer_restore_has_no_login_elevation_or_membership(schema):
    statements = backup.restore_role_sql(schema).split("; ")
    model_roles = [s for s in statements if "ec_model_usage_definer" in s]
    assert model_roles == [
        "CREATE ROLE ec_model_usage_definer NOLOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS;"
    ]
    with patch.object(backup, "run", return_value="1") as sql:
        backup.verify_model_usage_role(sql, schema)
    sql.assert_called_once()
    query = sql.call_args.args[0]
    for flag in (
        "rolcanlogin",
        "rolsuper",
        "rolbypassrls",
        "rolcreaterole",
        "rolcreatedb",
        "rolreplication",
    ):
        assert flag in query
    assert "m.member=r.oid OR m.roleid=r.oid" in query


@pytest.fixture
def private_recording(recording):
    events, copies = recording
    deployments = [deployment(name) for name in sorted(backup.PRIVATE_WRITERS)]
    deployments += [deployment("ec-dev-redis"), deployment(backup.PUBLIC_TUNNEL, 2)]
    original_k = backup.k.side_effect

    def cluster(*args, **kwargs):
        if args[:2] == ("get", "deployments"):
            events.append(args)
            return SimpleNamespace(stdout=json.dumps({"items": deployments}))
        if args[0] == "get" and args[1].startswith("deployment/"):
            events.append(args)
            name = args[1].split("/", 1)[1]
            return SimpleNamespace(
                stdout=json.dumps(next(d for d in deployments if d["metadata"]["name"] == name))
            )
        if args[0] in ("scale", "rollout"):
            events.append(args)
            if args[0] == "scale":
                name = args[1].split("/", 1)[1]
                item = next(d for d in deployments if d["metadata"]["name"] == name)
                count = int(args[2].split("=", 1)[1])
                item["spec"]["replicas"] = count
                item["status"] = {
                    "observedGeneration": 1,
                    "replicas": count,
                    "updatedReplicas": count,
                    "availableReplicas": count,
                    "readyReplicas": count,
                }
            return SimpleNamespace(stdout=b"")
        return original_k(*args, **kwargs)

    backup.k.side_effect = cluster
    return events, copies, deployments


def test_private_backup_quiesces_public_edge_first_and_records_bounded_recovery(private_recording):
    events, copies, _ = private_recording
    backup.backup(profile="private", hold_stopped=True)
    scales = [event for event in events if event[0] == "scale"]
    assert scales[0] == ("scale", "deployment/" + backup.PUBLIC_TUNNEL, "--replicas=0")
    edge_wait = next(
        i
        for i, event in enumerate(events)
        if event[0] == "wait" and backup.PUBLIC_TUNNEL in event[4]
    )
    first_writer = next(
        i
        for i, event in enumerate(events)
        if event[0] == "scale" and event[1] != "deployment/" + backup.PUBLIC_TUNNEL
    )
    assert edge_wait < first_writer
    assert all(event[1] != "deployment/ec-dev-redis" for event in scales)
    metadata = json.loads(copies["recovery.json"])
    assert metadata["version"] == 2
    assert metadata["deployment_profile"] == "private"
    assert metadata["replicas"][backup.PUBLIC_TUNNEL] == 2
    assert (
        metadata["deployment_uids"][backup.PUBLIC_TUNNEL]
        == deployment(backup.PUBLIC_TUNNEL)["metadata"]["uid"]
    )
    assert metadata["template_sha256"][backup.PUBLIC_TUNNEL] == backup._template_hash(
        deployment(backup.PUBLIC_TUNNEL)
    )
    backup._validate_recovery(metadata, backup.BACKUP_ROOT + metadata["id"])


def test_private_regular_backup_restores_edge_only_after_temporal_and_writers_ready(
    private_recording,
):
    events, copies, _ = private_recording
    backup.backup(profile="private")
    scales = [event for event in events if event[0] == "scale"]
    restored = [event for event in scales if event[2] != "--replicas=0"]
    assert restored[0][1].startswith("deployment/ec-dev-temporal-")
    assert restored[-1][1:3] == ("deployment/" + backup.PUBLIC_TUNNEL, "--replicas=2")
    edge_index = events.index(restored[-1])
    waited = {event[2] for event in events[:edge_index] if event[0] == "rollout"}
    assert waited == {"deployment/" + name for name in backup.PRIVATE_WRITERS}
    assert json.loads(copies["manifest.json"])["resume_required"] is False


@pytest.mark.parametrize(
    "name,count",
    [
        ("unknown-process", 1),
        ("events-concierge-admin", 1),
        ("events-concierge-operator-api", 1),
        ("events-concierge-api", 2),
        (backup.PUBLIC_TUNNEL, 1),
        (backup.PUBLIC_TUNNEL, True),
    ],
)
def test_private_invalid_inventory_or_capacity_fails_before_cloud_or_scale(
    private_recording, name, count
):
    events, copies, deployments = private_recording
    deployments[:] = [d for d in deployments if d["metadata"]["name"] != name]
    deployments.append(deployment(name, count))
    with pytest.raises(SystemExit):
        backup.backup(profile="private")
    assert not copies
    assert all(event[0] != "scale" for event in events)


@pytest.mark.parametrize("failure", ["scale", "readiness"])
def test_private_failed_writer_recovery_keeps_edge_stopped(private_recording, failure):
    events, _, deployments = private_recording
    original = backup.k.side_effect

    def cluster(*args, **kwargs):
        if failure == "scale" and args[:3] == (
            "scale",
            "deployment/events-concierge-api",
            "--replicas=1",
        ):
            raise subprocess.CalledProcessError(1, args, stderr=b"Forbidden")
        if failure == "readiness" and args[:3] == (
            "rollout",
            "status",
            "deployment/events-concierge-api",
        ):
            raise subprocess.TimeoutExpired(args, 190)
        return original(*args, **kwargs)

    backup.k.side_effect = cluster
    with pytest.raises(ExceptionGroup):
        backup.backup(profile="private")
    edge = next(d for d in deployments if d["metadata"]["name"] == backup.PUBLIC_TUNNEL)
    assert edge["spec"]["replicas"] == 0
    assert not any(
        event[:3] == ("scale", "deployment/" + backup.PUBLIC_TUNNEL, "--replicas=2")
        for event in events
    )
    assert any(
        event[:3] == ("scale", "deployment/events-concierge-temporal-catalog", "--replicas=1")
        for event in events
    )


@pytest.mark.parametrize(
    "change", ["edge_uid", "edge_template", "schema", "new_writer", "new_known_writer"]
)
def test_private_resume_refuses_changed_release_before_any_scale(private_recording, change):
    events, copies, deployments = private_recording
    backup.backup(profile="private", hold_stopped=True)
    metadata = json.loads(copies["recovery.json"])
    uri = backup.BACKUP_ROOT + metadata["id"]
    edge = next(d for d in deployments if d["metadata"]["name"] == backup.PUBLIC_TUNNEL)
    if change == "edge_uid":
        edge["metadata"]["uid"] = str(uuid.uuid4())
    elif change == "edge_template":
        edge["spec"]["template"]["spec"]["containers"][0]["image"] = "new-edge"
    elif change == "new_writer":
        deployments.append(deployment("events-concierge-admin"))
    elif change == "new_known_writer":
        # A saved private inventory without an edge must not resume after adding one.
        metadata["replicas"].pop(backup.PUBLIC_TUNNEL)
        metadata["deployment_uids"].pop(backup.PUBLIC_TUNNEL)
        metadata["template_sha256"].pop(backup.PUBLIC_TUNNEL)
        copies["recovery.json"] = json.dumps(metadata).encode()
    events.clear()
    with (
        patch.object(
            backup,
            "_read_schema",
            return_value="0207" if change == "schema" else metadata["schema"],
        ),
        pytest.raises((RuntimeError, SystemExit)),
    ):
        backup.resume(uri)
    assert all(event[0] != "scale" for event in events)


def test_private_backup_preserves_stopped_worker_and_edge_counts(private_recording):
    events, copies, deployments = private_recording
    stopped = {backup.PUBLIC_TUNNEL, "events-concierge-account-erasure"}
    for item in deployments:
        if item["metadata"]["name"] in stopped:
            item["spec"]["replicas"] = 0
    backup.backup(profile="private")
    metadata = json.loads(copies["recovery.json"])
    assert all(metadata["replicas"][name] == 0 for name in stopped)
    assert all(d["spec"]["replicas"] == 0 for d in deployments if d["metadata"]["name"] in stopped)
    assert not any(event[0] == "rollout" for event in events)


PRIVATE_OPERATORS = {"events-concierge-operator-api", "events-concierge-operator-frontend"}


@pytest.fixture
def operator_recording(private_recording):
    events, copies, deployments = private_recording
    deployments.extend(deployment(name) for name in sorted(PRIVATE_OPERATORS))
    return events, copies, deployments


def test_private_backup_requires_explicit_operator_inventory_opt_in(operator_recording):
    events, copies, _ = operator_recording
    with pytest.raises(SystemExit):
        backup.backup(profile="private")
    assert not copies
    assert not any(event[0] == "scale" for event in events)


@pytest.mark.parametrize("missing", [None, *sorted(PRIVATE_OPERATORS)])
def test_operator_backup_requires_both_deployments_before_upload_or_scale(
    private_recording, missing
):
    events, copies, deployments = private_recording
    if missing is not None:
        deployments.extend(deployment(name) for name in PRIVATE_OPERATORS - {missing})
    with pytest.raises(SystemExit):
        backup.backup(profile="private", operator=True)
    assert not copies
    assert not any(event[0] == "scale" for event in events)


@pytest.mark.parametrize("name", sorted(PRIVATE_OPERATORS))
def test_operator_backup_rejects_extra_replicas_before_upload_or_scale(operator_recording, name):
    events, copies, deployments = operator_recording
    next(d for d in deployments if d["metadata"]["name"] == name)["spec"]["replicas"] = 2
    with pytest.raises(SystemExit):
        backup.backup(profile="private", operator=True)
    assert not copies
    assert not any(event[0] == "scale" for event in events)


def test_operator_option_does_not_expand_other_private_inventory(operator_recording):
    events, copies, deployments = operator_recording
    deployments.append(deployment("events-concierge-admin"))
    with pytest.raises(SystemExit):
        backup.backup(profile="private", operator=True)
    assert not copies
    assert not any(event[0] == "scale" for event in events)


def test_operator_backup_records_original_counts_identities_and_templates(operator_recording):
    events, copies, deployments = operator_recording
    stopped = "events-concierge-operator-api"
    next(d for d in deployments if d["metadata"]["name"] == stopped)["spec"]["replicas"] = 0
    backup.backup(profile="private", operator=True, hold_stopped=True)
    metadata = json.loads(copies["recovery.json"])
    manifest = json.loads(copies["manifest.json"])
    for name in PRIVATE_OPERATORS:
        original = deployment(name, 0 if name == stopped else 1)
        assert metadata["replicas"][name] == original["spec"]["replicas"]
        assert metadata["deployment_uids"][name] == original["metadata"]["uid"]
        assert metadata["template_sha256"][name] == backup._template_hash(original)
        assert manifest["quiesced_deployments"][name] == original["spec"]["replicas"]
    backup._validate_recovery(metadata, backup.BACKUP_ROOT + metadata["id"])
    scales = [event for event in events if event[0] == "scale"]
    assert scales[0][1] == "deployment/" + backup.PUBLIC_TUNNEL
    assert all(event[2] == "--replicas=0" for event in scales)
    operator_scale = next(
        event for event in scales if event[1] == "deployment/" + backup.OPERATOR_FRONTEND
    )
    operator_wait = next(
        i
        for i, event in enumerate(events)
        if event[0] == "wait" and backup.OPERATOR_FRONTEND in event[4]
    )
    first_writer = next(
        i
        for i, event in enumerate(events)
        if event[0] == "scale"
        and event[1]
        not in {
            "deployment/" + backup.PUBLIC_TUNNEL,
            "deployment/" + backup.OPERATOR_FRONTEND,
        }
    )
    assert events.index(operator_scale) < operator_wait < first_writer


def test_operator_recovery_derives_saved_allowlist_and_opens_consumer_edge_last(operator_recording):
    events, copies, deployments = operator_recording
    backup.backup(profile="private", operator=True, hold_stopped=True)
    metadata = json.loads(copies["recovery.json"])
    events.clear()
    backup.resume(backup.BACKUP_ROOT + metadata["id"])
    restored = [event for event in events if event[0] == "scale" and event[2] != "--replicas=0"]
    assert restored[0][1].startswith("deployment/ec-dev-temporal-")
    assert restored[-1][1:3] == ("deployment/" + backup.PUBLIC_TUNNEL, "--replicas=2")
    assert restored[-2][1:3] == ("deployment/" + backup.OPERATOR_FRONTEND, "--replicas=1")
    before_edge = events[: events.index(restored[-1])]
    for name in PRIVATE_OPERATORS:
        assert any(
            event[:3] == ("rollout", "status", "deployment/" + name) for event in before_edge
        )
        assert (
            next(d for d in deployments if d["metadata"]["name"] == name)["spec"]["replicas"] == 1
        )


@pytest.mark.parametrize("missing", sorted(PRIVATE_OPERATORS))
def test_recovery_metadata_rejects_a_half_operator_pair_without_cluster_writes(
    operator_recording, missing
):
    events, copies, _ = operator_recording
    backup.backup(profile="private", operator=True, hold_stopped=True)
    metadata = json.loads(copies["recovery.json"])
    for field in ("replicas", "deployment_uids", "template_sha256"):
        metadata[field].pop(missing)
    copies["recovery.json"] = json.dumps(metadata).encode()
    events.clear()
    with pytest.raises(SystemExit):
        backup.resume(backup.BACKUP_ROOT + metadata["id"])
    assert not any(event[0] == "scale" for event in events)


@pytest.mark.parametrize("name", sorted(PRIVATE_OPERATORS))
@pytest.mark.parametrize("change", ["uid", "template"])
def test_operator_resume_refuses_replacement_or_unreviewed_template_before_scale(
    operator_recording, name, change
):
    events, copies, deployments = operator_recording
    backup.backup(profile="private", operator=True, hold_stopped=True)
    metadata = json.loads(copies["recovery.json"])
    item = next(d for d in deployments if d["metadata"]["name"] == name)
    if change == "uid":
        item["metadata"]["uid"] = str(uuid.uuid4())
    else:
        item["spec"]["template"]["spec"]["containers"][0]["image"] = "unreviewed-operator-image"
    events.clear()
    with pytest.raises(RuntimeError):
        backup.resume(backup.BACKUP_ROOT + metadata["id"])
    assert not any(event[0] == "scale" for event in events)


def test_operator_backup_is_private_only_before_any_cluster_or_cloud_io():
    with (
        patch.object(backup, "k") as cluster,
        patch.object(backup, "gc") as cloud,
        patch.object(backup, "_check_context") as context,
        pytest.raises(SystemExit, match="private"),
    ):
        backup.backup(operator=True)
    cluster.assert_not_called()
    cloud.assert_not_called()
    context.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["operator-api-scale", "operator-api-readiness", "operator-frontend-scale"]
)
def test_failed_operator_recovery_keeps_operator_and_consumer_edges_stopped(
    operator_recording, failure
):
    events, _, deployments = operator_recording
    original = backup.k.side_effect

    def cluster(*args, **kwargs):
        failed_call = {
            "operator-api-scale": (
                "scale",
                "deployment/events-concierge-operator-api",
                "--replicas=1",
            ),
            "operator-api-readiness": (
                "rollout",
                "status",
                "deployment/events-concierge-operator-api",
            ),
            "operator-frontend-scale": (
                "scale",
                "deployment/" + backup.OPERATOR_FRONTEND,
                "--replicas=1",
            ),
        }[failure]
        if args[:3] == failed_call:
            raise subprocess.CalledProcessError(1, args, stderr=b"Forbidden")
        return original(*args, **kwargs)

    backup.k.side_effect = cluster
    with pytest.raises(ExceptionGroup):
        backup.backup(profile="private", operator=True)
    for name in (backup.PUBLIC_TUNNEL, backup.OPERATOR_FRONTEND):
        assert (
            next(d for d in deployments if d["metadata"]["name"] == name)["spec"]["replicas"] == 0
        )
    assert not any(
        event[:3] == ("scale", "deployment/" + backup.PUBLIC_TUNNEL, "--replicas=2")
        for event in events
    )


def test_operator_recovery_preserves_originally_stopped_edges_and_workers(operator_recording):
    _, copies, deployments = operator_recording
    stopped = PRIVATE_OPERATORS | {backup.PUBLIC_TUNNEL, "events-concierge-account-erasure"}
    for item in deployments:
        if item["metadata"]["name"] in stopped:
            item["spec"]["replicas"] = 0
    backup.backup(profile="private", operator=True)
    metadata = json.loads(copies["recovery.json"])
    assert all(metadata["replicas"][name] == 0 for name in stopped)
    assert all(d["spec"]["replicas"] == 0 for d in deployments if d["metadata"]["name"] in stopped)


def test_backup_cli_operator_flag_records_the_exact_private_pair(operator_recording):
    _, copies, _ = operator_recording
    cluster, cloud = backup.k.side_effect, backup.gc.side_effect
    path = Path(__file__).resolve().parents[2] / "scripts/development/backup.py"

    def command(args, **kwargs):
        kwargs.pop("check")
        if args[0] == backup.K:
            return cluster(*args[4:], **kwargs)
        assert args[:2] == ["gcloud", "storage"]
        return cloud(*args[2:-2], **kwargs)

    with (
        patch.object(
            sys,
            "argv",
            [str(path), "backup", "--profile", "private", "--operator", "--hold-stopped"],
        ),
        patch.object(backup.subprocess, "run", side_effect=command),
    ):
        runpy.run_path(str(path), run_name="__main__")
    metadata = json.loads(copies["recovery.json"])
    assert set(metadata["replicas"]) & PRIVATE_OPERATORS == PRIVATE_OPERATORS


@pytest.mark.parametrize("action", ["verify", "resume"])
def test_backup_cli_refuses_operator_override_for_saved_recovery(action):
    path = Path(__file__).resolve().parents[2] / "scripts/development/backup.py"
    with (
        patch.object(sys, "argv", [str(path), action, RECOVERY_URI, "--operator"]),
        patch.object(backup.subprocess, "run") as command,
        patch.object(backup.subprocess, "check_output") as output,
        pytest.raises(SystemExit) as error,
    ):
        runpy.run_path(str(path), run_name="__main__")
    assert error.value.code == 2
    command.assert_not_called()
    output.assert_not_called()


@pytest.mark.parametrize("edge_mode", ["absent", "stopped"])
def test_private_automatic_recovery_checks_inventory_without_a_running_edge(
    private_recording, edge_mode
):
    events, _, deployments = private_recording
    if edge_mode == "absent":
        deployments[:] = [d for d in deployments if d["metadata"]["name"] != backup.PUBLIC_TUNNEL]
    else:
        edge = next(d for d in deployments if d["metadata"]["name"] == backup.PUBLIC_TUNNEL)
        edge["spec"]["replicas"] = 0
    original = backup.gc.side_effect
    injected = False

    def cloud(*args, **kwargs):
        nonlocal injected
        if args[0] == "cp" and args[-1].endswith(".dump") and not injected:
            injected = True
            name = backup.PUBLIC_TUNNEL if edge_mode == "absent" else "events-concierge-admin"
            deployments.append(deployment(name, 2 if edge_mode == "absent" else 1))
        return original(*args, **kwargs)

    backup.gc.side_effect = cloud
    with pytest.raises(RuntimeError, match="inventory changed"):
        backup.backup(profile="private")
    assert injected
    assert all(event[2] == "--replicas=0" for event in events if event[0] == "scale")


@pytest.mark.parametrize("change", ["replicas", "template", "schema"])
def test_public_resume_rechecks_exact_recovery_after_rollout_wait(private_recording, change):
    _, _, deployments = private_recording
    original = backup.k.side_effect
    api = next(d for d in deployments if d["metadata"]["name"] == "events-concierge-api")

    def cluster(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:3] == ("rollout", "status", "deployment/events-concierge-api"):
            if change == "replicas":
                api["spec"]["replicas"] = 2
            elif change == "template":
                api["spec"]["template"]["spec"]["containers"][0]["image"] = "new-image"
            else:
                backup._read_schema.return_value = "0207"
        return result

    backup.k.side_effect = cluster
    with (
        patch.object(backup, "_read_schema", return_value="0193"),
        pytest.raises(ExceptionGroup),
    ):
        backup.backup(profile="private")
    edge = next(d for d in deployments if d["metadata"]["name"] == backup.PUBLIC_TUNNEL)
    assert edge["spec"]["replicas"] == 0


def test_model_usage_definer_restore_rejects_missing_or_unsafe_role():
    with (
        patch.object(backup, "run", return_value="0") as sql,
        pytest.raises(AssertionError, match="model usage definer"),
    ):
        backup.verify_model_usage_role(sql, "0205")


def test_restored_role_verification_keeps_operator_isolation_failure_fatal():
    with (
        patch.object(backup, "run", return_value="1") as sql,
        pytest.raises(AssertionError, match="operator roles bypass consumer isolation"),
    ):
        backup.verify_restored_roles(sql, "0205")
    sql.assert_called_once()


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
