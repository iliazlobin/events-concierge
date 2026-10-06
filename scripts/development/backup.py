#!/usr/bin/env python3
"""Manual quiesced development backup. Restores are always into disposable local databases."""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import uuid

from events_concierge.deployment.development_targets import TARGETS

NS = "events-concierge-dev"
PROJECT = TARGETS["shared"].project
ACCOUNT = "iliazlobin27@gmail.com"
K = os.environ.get("KUBECTL", "kubectl")
CONTEXT = TARGETS["shared"].context
BACKUP_ROOT = "gs://" + TARGETS["shared"].backup_bucket + "/"
PAYLOAD_ROOT = "gs://" + TARGETS["shared"].payload_bucket
TARGET_NAME = "shared"
RECOVERY_FILE = "recovery.json"
PRIVATE_RECOVERY_VERSION = 2
WRITERS = {
    "events-concierge-" + name
    for name in (
        "api",
        "admin",
        "frontend",
        "temporal-transactional",
        "temporal-catalog",
        "ingestion-executor",
        "request-starter",
        "notifier",
        "account-erasure",
        "change-delivery",
    )
} | {"ec-dev-temporal-" + name for name in ("frontend", "history", "matching", "worker")}
PRIVATE_WRITERS = WRITERS - {
    "events-concierge-" + name
    for name in (
        "admin",
        "temporal-transactional",
        "request-starter",
        "notifier",
        "change-delivery",
    )
}
PUBLIC_TUNNEL = "events-concierge-public-tunnel"
RESUME_RETRY_DELAYS = (2, 4, 8)
OPERATOR_ROLE_SCHEMA = 182
MODEL_USAGE_ROLE_SCHEMA = 201
SNAPSHOT_SQL = "SELECT json_build_object('tenants',(SELECT count(*) FROM tenants),'requests',(SELECT count(*) FROM event_requests),'schema',(SELECT version_num FROM alembic_version))::text"


def select_target(name):
    # One destination per CLI process, selected before any cloud or Kubernetes IO.
    global PROJECT, CONTEXT, BACKUP_ROOT, PAYLOAD_ROOT, TARGET_NAME  # noqa: PLW0603
    destination = TARGETS[name]
    PROJECT, CONTEXT = destination.project, destination.context
    BACKUP_ROOT = "gs://" + destination.backup_bucket + "/"
    PAYLOAD_ROOT = "gs://" + destination.payload_bucket
    TARGET_NAME = name


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def k(*args, **kw):
    return run([K, "--context=" + CONTEXT, "-n", NS, *args], **kw)


def gc(*args, **kw):
    return run(["gcloud", "storage", *args, "--account=" + ACCOUNT, "--project=" + PROJECT], **kw)


def _check_context():
    context = subprocess.check_output(
        [K, "config", "current-context"], text=True, timeout=10
    ).strip()
    if context != CONTEXT:
        raise SystemExit("Wrong cluster context")


def _check_scheduled_writers_quiet():
    resources = json.loads(
        k("get", "cronjobs,jobs", "-o", "json", capture_output=True, timeout=30).stdout
    )["items"]
    for resource in resources:
        name = resource["metadata"]["name"]
        if resource["kind"] == "CronJob":
            if resource.get("spec", {}).get("suspend") is not True:
                raise SystemExit("Suspend scheduled jobs before backup: " + name)
        elif resource["kind"] == "Job":
            terminal = any(
                condition.get("type") in {"Complete", "Failed"}
                and condition.get("status") == "True"
                for condition in resource.get("status", {}).get("conditions", [])
            )
            if not terminal:
                raise SystemExit("Wait for or resolve unfinished jobs before backup: " + name)
        else:
            raise SystemExit("Unexpected scheduled writer resource")


def _backup_id(uri):
    match = re.fullmatch(re.escape(BACKUP_ROOT) + r"(\d{8}T\d{6}Z-[0-9a-f]{8})", uri)
    if not match:
        raise SystemExit("Use an exact development backup prefix without a trailing slash")
    try:
        datetime.datetime.strptime(match[1].split("-")[0], "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise SystemExit("Invalid development backup date") from error
    return match[1]


def _validate_replicas(desired, *, profile="development"):
    if profile not in ("development", "private"):
        raise SystemExit("Unknown deployment profile")
    allowed = PRIVATE_WRITERS | {PUBLIC_TUNNEL} if profile == "private" else WRITERS
    if not isinstance(desired, dict) or not desired or set(desired) - allowed:
        raise SystemExit("Recovery contains unknown or missing writer deployment names")
    # Writers remain singletons; only the explicitly owned public edge has two
    # replicas. Recovery metadata cannot silently change the agreed capacity.
    if any(
        type(count) is not int or count not in (0, 2 if name == PUBLIC_TUNNEL else 1)
        for name, count in desired.items()
    ):
        raise SystemExit("Recovery requires bounded integer replicas: writers 0/1, public edge 0/2")


def _read_schema():
    return (
        k(
            "exec",
            "ec-dev-application-postgres-0",
            "--request-timeout=20s",
            "--",
            "psql",
            "-U",
            "ec_owner",
            "-d",
            "events",
            "-Atc",
            "select version_num from alembic_version",
            capture_output=True,
            timeout=30,
        )
        .stdout.decode()
        .strip()
    )


def _recovery_metadata(ident, writers, schema, *, profile="development"):
    desired = {d["metadata"]["name"]: d["spec"]["replicas"] for d in writers}
    _validate_replicas(desired, profile=profile)
    uids = {d["metadata"]["name"]: d["metadata"]["uid"] for d in writers}
    metadata = {
        "version": PRIVATE_RECOVERY_VERSION if profile == "private" else 1,
        "id": ident,
        "context": CONTEXT,
        "project": PROJECT,
        "namespace": NS,
        "schema": schema,
        "replicas": desired,
        "deployment_uids": uids,
        "template_sha256": {d["metadata"]["name"]: _template_hash(d) for d in writers},
    }
    if profile == "private":
        metadata["deployment_profile"] = profile
    _validate_recovery(metadata, BACKUP_ROOT + ident)
    return metadata


def _validate_recovery(metadata, uri):
    if not isinstance(metadata, dict):
        raise SystemExit("Invalid recovery metadata fields")
    fields = {
        "version",
        "id",
        "context",
        "project",
        "namespace",
        "schema",
        "replicas",
        "deployment_uids",
        "template_sha256",
    }
    if metadata.get("version") == PRIVATE_RECOVERY_VERSION:
        fields.add("deployment_profile")
    if set(metadata) != fields:
        raise SystemExit("Invalid recovery metadata fields")
    if (
        type(metadata["version"]) is not int
        or metadata["version"] not in (1, PRIVATE_RECOVERY_VERSION)
        or (
            metadata["version"] == PRIVATE_RECOVERY_VERSION
            and metadata["deployment_profile"] != "private"
        )
        or metadata["id"] != _backup_id(uri)
        or metadata["context"] != CONTEXT
        or metadata["project"] != PROJECT
        or metadata["namespace"] != NS
    ):
        raise SystemExit("Recovery metadata does not match this development backup and cluster")
    if not isinstance(metadata["schema"], str) or not re.fullmatch(r"[0-9]{4}", metadata["schema"]):
        raise SystemExit("Invalid recovery schema version")
    _validate_replicas(
        metadata["replicas"], profile=metadata.get("deployment_profile", "development")
    )
    uids = metadata["deployment_uids"]
    if not isinstance(uids, dict) or set(uids) != set(metadata["replicas"]):
        raise SystemExit("Recovery deployment identities do not match replica records")
    for uid in uids.values():
        try:
            valid = isinstance(uid, str) and str(uuid.UUID(uid)) == uid
        except ValueError:
            valid = False
        if not valid:
            raise SystemExit("Invalid recovery deployment identity")
    if len(set(uids.values())) != len(uids):
        raise SystemExit("Duplicate recovery deployment identities")
    templates = metadata["template_sha256"]
    if (
        not isinstance(templates, dict)
        or set(templates) != set(uids)
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in templates.values()
        )
    ):
        raise SystemExit("Invalid recovery deployment template fingerprints")


def _template_hash(deployment):
    # Retain a fingerprint only; the template itself can contain secret values.
    payload = json.dumps(deployment["spec"]["template"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_current_deployment(deployment, name, recovery):
    if (
        deployment.get("metadata", {}).get("uid") != recovery["deployment_uids"][name]
        or _template_hash(deployment) != recovery["template_sha256"][name]
    ):
        raise RuntimeError(
            "Recovery refused: deployment was replaced or its workload changed: " + name
        )
    version = deployment["metadata"].get("resourceVersion")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+", version):
        raise RuntimeError("Recovery refused: invalid deployment resource version: " + name)
    return version


def _validate_private_inventory(current, desired):
    if set(current) - {"ec-dev-redis"} != set(desired):
        raise RuntimeError("Recovery refused: private deployment inventory changed")


def _preflight_private_recovery(desired, recovery):
    items = json.loads(
        k("get", "deployments", "-o", "json", capture_output=True, timeout=30).stdout
    )["items"]
    current = {d["metadata"]["name"]: d for d in items}
    _validate_private_inventory(current, desired)
    for name in desired:
        _validate_current_deployment(current[name], name, recovery)
    return current


def resume(uri):
    """Recover a stopped backup attempt, never restore data or older images."""
    _backup_id(uri)
    _check_context()
    metadata = json.loads(gc("cat", uri + "/" + RECOVERY_FILE, capture_output=True).stdout)
    _validate_recovery(metadata, uri)
    # Validate all identities before the first write. Migration/rollout may have
    # replaced a Deployment; stale recovery must not start its replacement.
    items = json.loads(
        k("get", "deployments", "-o", "json", capture_output=True, timeout=30).stdout
    )["items"]
    current = {d["metadata"]["name"]: d for d in items}
    if metadata.get("deployment_profile") == "private":
        _validate_private_inventory(current, metadata["replicas"])
    for name in metadata["replicas"]:
        if name not in current:
            raise SystemExit("Recovery refused: a saved deployment is missing: " + name)
        _validate_current_deployment(current[name], name, metadata)
    _resume(metadata["replicas"], recovery=metadata)
    print("Original replica counts restored:", uri)
    print("Check deployment readiness before reopening access.")


def _quiesce(deployments):
    # Close the public edge before writers; let application shutdown finish while
    # Temporal remains available. PostgreSQL and Redis stay running.
    for phase_name in ("edge", "application", "temporal"):
        phase = [d for d in deployments if _resume_phase(d["metadata"]["name"]) == phase_name]
        for d in phase:
            k(
                "scale",
                "deployment/" + d["metadata"]["name"],
                "--replicas=0",
                stdout=subprocess.DEVNULL,
                timeout=30,
            )
        for d in phase:
            # Deployment readiness can report success before terminating pods exit.
            selector = ",".join(
                f"{key}={val}" for key, val in d["spec"]["selector"]["matchLabels"].items()
            )
            k(
                "wait",
                "--for=delete",
                "pod",
                "-l",
                selector,
                "--timeout=180s",
                stdout=subprocess.DEVNULL,
                timeout=190,
            )


def _transient_scale_error(error):
    output = error.stderr or b""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    output = output.lower()
    return any(
        message in output
        for message in (
            "no such host",
            "temporary failure in name resolution",
            "server misbehaving",
            "i/o timeout",
            "tls handshake timeout",
            "connection reset",
            "connection refused",
            "context deadline exceeded",
            "timeout awaiting response headers",
            "unexpected eof",
            "unable to connect to the server: eof",
            "too many requests",
            "toomanyrequests",
            "serviceunavailable",
            "service unavailable",
            "internalerror",
            "internal server error",
            "gateway timeout",
            "bad gateway",
        )
    )


def _check_recovery_schema(recovery):
    for attempt in range(len(RESUME_RETRY_DELAYS) + 1):
        try:
            if _read_schema() != recovery["schema"]:
                raise RuntimeError(
                    "Recovery refused: application schema changed; use compatible-image recovery"
                )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            transient = isinstance(error, subprocess.TimeoutExpired) or _transient_scale_error(
                error
            )
            if not transient or attempt == len(RESUME_RETRY_DELAYS):
                raise
            delay = RESUME_RETRY_DELAYS[attempt]
            print(f"Transient schema recovery check error; retrying in {delay}s", flush=True)
            time.sleep(delay)


def _resume_phase(name):
    if name == PUBLIC_TUNNEL:
        return "edge"
    return "temporal" if name.startswith("ec-dev-temporal-") else "application"


def _wait_before_public_resume(desired, recovery):
    if any(desired.get("events-concierge-" + name) != 1 for name in ("api", "frontend")):
        raise RuntimeError("Public edge recovery requires running API and frontend replicas")
    for name, count in sorted(desired.items()):
        if count and name != PUBLIC_TUNNEL:
            k(
                "rollout",
                "status",
                "deployment/" + name,
                "--timeout=180s",
                "--request-timeout=20s",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=190,
            )
    items = json.loads(
        k("get", "deployments", "-o", "json", capture_output=True, timeout=30).stdout
    )["items"]
    current = {d["metadata"]["name"]: d for d in items}
    _validate_private_inventory(current, desired)
    # Recheck identity, exact replicas and readiness after waiting, before opening
    # the edge. Rollout status alone could accept an independently changed count.
    for name, count in desired.items():
        if name == PUBLIC_TUNNEL:
            continue
        deployment = current.get(name)
        if deployment is None:
            raise RuntimeError("Public edge recovery is missing a saved writer: " + name)
        _validate_current_deployment(deployment, name, recovery)
        status = deployment.get("status", {})
        if (
            deployment["spec"].get("replicas") != count
            or status.get("observedGeneration", 0) < deployment["metadata"].get("generation", 1)
            or any(
                status.get(field, 0) != count
                for field in ("replicas", "updatedReplicas", "availableReplicas", "readyReplicas")
            )
            or status.get("unavailableReplicas", 0)
        ):
            raise RuntimeError("Public edge recovery requires exact ready writer replicas: " + name)
    _check_recovery_schema(recovery)


def _resume(desired, *, recovery=None):
    profile = recovery.get("deployment_profile", "development") if recovery else "development"
    _validate_replicas(desired, profile=profile)
    if recovery is not None:
        _check_recovery_schema(recovery)
    if profile == "private":
        _preflight_private_recovery(desired, recovery)
    failures = []
    # Resume Temporal, then application writers, then the public edge. Attempt all
    # writers after a failure, but never reopen public access to incomplete recovery.
    order = {"temporal": 0, "application": 1, "edge": 2}
    for name in sorted(desired, key=lambda n: order[_resume_phase(n)]):
        if name == PUBLIC_TUNNEL and desired[name]:
            if failures:
                failures.append(
                    RuntimeError("Public edge remains stopped after writer recovery failure")
                )
                continue
            try:
                _wait_before_public_resume(desired, recovery)
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                RuntimeError,
            ) as error:
                failures.append(error)
                continue
        for attempt in range(len(RESUME_RETRY_DELAYS) + 1):
            try:
                preconditions = []
                if recovery is not None:
                    deployment = json.loads(
                        k(
                            "get",
                            "deployment/" + name,
                            "-o",
                            "json",
                            "--request-timeout=20s",
                            capture_output=True,
                            timeout=30,
                        ).stdout
                    )
                    version = _validate_current_deployment(deployment, name, recovery)
                    preconditions = ["--resource-version=" + version]
                k(
                    "scale",
                    "deployment/" + name,
                    "--replicas=" + str(desired[name]),
                    "--request-timeout=20s",
                    *preconditions,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                transient = isinstance(error, subprocess.TimeoutExpired) or _transient_scale_error(
                    error
                )
                if not transient or attempt == len(RESUME_RETRY_DELAYS):
                    failures.append(error)
                    break
                delay = RESUME_RETRY_DELAYS[attempt]
                print(f"Transient recovery error for {name}; retrying in {delay}s", flush=True)
                time.sleep(delay)
            except (RuntimeError, ValueError, KeyError) as error:
                failures.append(error)
                break
    if failures:
        raise ExceptionGroup("Could not restore every original replica count", failures)


def _persist_recovery(folder, dest, recovery):
    # Persist and verify before stopping anything. The record contains no
    # environment values or secret contents; workload templates are hashed.
    recovery_path = folder / RECOVERY_FILE
    recovery_path.write_text(json.dumps(recovery, indent=2))
    recovery_path.chmod(0o600)
    gc("cp", str(recovery_path), dest + "/" + RECOVERY_FILE, stdout=subprocess.DEVNULL)
    saved_recovery = json.loads(gc("cat", dest + "/" + RECOVERY_FILE, capture_output=True).stdout)
    _validate_recovery(saved_recovery, dest)
    if saved_recovery != recovery:
        raise SystemExit("Recovery metadata read-back mismatch; workloads were not stopped")
    print("Recovery metadata saved:", dest + "/" + RECOVERY_FILE, flush=True)
    print(
        f"If backup recovery is interrupted: .venv/bin/python scripts/development/backup.py resume {dest} --target {TARGET_NAME}",
        flush=True,
    )


def _check_backup_target():
    _check_context()
    _check_scheduled_writers_quiet()


def _object_inventory(folder):
    return {
        path.relative_to(folder).as_posix(): {
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _copy_payload_snapshot(folder, dest):
    gc("rsync", "--recursive", PAYLOAD_ROOT, dest + "/payloads", stdout=subprocess.DEVNULL)
    objects = folder / "payloads"
    objects.mkdir(mode=0o700)
    # Hash the saved snapshot, covering every durable media and claim-check object.
    gc("rsync", "--recursive", dest + "/payloads", str(objects), stdout=subprocess.DEVNULL)
    return _object_inventory(objects)


def _verify_object_inventory(folder, expected):
    actual = _object_inventory(folder)
    if expected is None:
        if any(not name.endswith(".payload") for name in actual):
            raise SystemExit("Legacy backup cannot verify non-payload media objects")
        return False
    if not isinstance(expected, dict) or actual != expected:
        raise SystemExit("Restored object inventory or checksum mismatch")
    return True


def backup(*, hold_stopped=False, profile="development"):
    _check_backup_target()
    ident = (
        datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    dest = BACKUP_ROOT + ident
    deployments = json.loads(
        k("get", "deployments", "-o", "json", capture_output=True, timeout=30).stdout
    )["items"]
    # This namespace is dedicated to the selected profile. Redis is a store, not
    # a writer to this snapshot. The private public edge is explicitly quiesced;
    # unknown/demo/operator Deployments fail validation before upload or scaling.
    desired = {d["metadata"]["name"]: d["spec"]["replicas"] for d in deployments}
    writers = [d for d in deployments if d["metadata"]["name"] != "ec-dev-redis"]
    quiesced = {d["metadata"]["name"]: d["spec"]["replicas"] for d in writers}
    recovery = _recovery_metadata(ident, writers, _read_schema(), profile=profile)
    with tempfile.TemporaryDirectory(prefix="ec-dev-backup-") as tmp:
        os.chmod(tmp, 0o700)
        folder = pathlib.Path(tmp)
        manifest = {
            "id": ident,
            "databases": {},
            "replicas": desired,
            "quiesced_deployments": quiesced,
            "resume_required": hold_stopped,
        }
        complete = False
        _persist_recovery(folder, dest, recovery)
        backup_error = None
        try:
            _quiesce(writers)
            _check_scheduled_writers_quiet()
            for store, user, dbs in [
                ("application", "ec_owner", ["events"]),
                ("temporal", "temporal", ["temporal", "temporal_visibility"]),
            ]:
                for db in dbs:
                    name = f"{store}-{db}.dump"
                    path = folder / name
                    with path.open("wb") as f:
                        k(
                            "exec",
                            f"ec-dev-{store}-postgres-0",
                            "--",
                            "pg_dump",
                            "-U",
                            user,
                            "-d",
                            db,
                            "-Fc",
                            stdout=f,
                        )
                    path.chmod(0o600)
                    manifest["databases"][name] = {
                        "store": store,
                        "database": db,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    gc("cp", str(path), dest + "/" + name, stdout=subprocess.DEVNULL)
            # Copy the current payload snapshot while writers are stopped; source versions remain in the versioned bucket.
            manifest["objects"] = _copy_payload_snapshot(folder, dest)
            manifest["images"] = {
                d["metadata"]["name"]: [
                    c["image"] for c in d["spec"]["template"]["spec"]["containers"]
                ]
                for d in deployments
            }
            manifest["application_snapshot"] = json.loads(
                k(
                    "exec",
                    "ec-dev-application-postgres-0",
                    "--",
                    "psql",
                    "-U",
                    "ec_owner",
                    "-d",
                    "events",
                    "-Atc",
                    SNAPSHOT_SQL,
                    capture_output=True,
                ).stdout
            )
            manifest["schema"] = (
                k(
                    "exec",
                    "ec-dev-application-postgres-0",
                    "--",
                    "psql",
                    "-U",
                    "ec_owner",
                    "-d",
                    "events",
                    "-Atc",
                    "select version_num from alembic_version",
                    capture_output=True,
                )
                .stdout.decode()
                .strip()
            )
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
            gc(
                "cp",
                str(folder / "manifest.json"),
                dest + "/manifest.json",
                stdout=subprocess.DEVNULL,
            )
            # A completion marker is written last; incomplete prefixes are never restore candidates.
            (folder / "COMPLETE").write_text(ident)
            gc("cp", str(folder / "COMPLETE"), dest + "/COMPLETE", stdout=subprocess.DEVNULL)
            complete = True
        except BaseException as error:
            backup_error = error
            raise
        finally:
            if not hold_stopped or not complete:
                try:
                    _resume(quiesced, recovery=recovery)
                except Exception as recovery_error:
                    print(
                        f"Replica recovery incomplete; retry: .venv/bin/python scripts/development/backup.py resume {dest} --target {TARGET_NAME}",
                        flush=True,
                    )
                    if backup_error is not None:
                        raise BaseExceptionGroup(
                            "Backup and replica recovery both failed",
                            [backup_error, recovery_error],
                        ) from None
                    raise
    print("Backup complete:", dest)
    if hold_stopped:
        print(
            "Application writers and Temporal remain stopped; restore replicas from manifest after cutover."
        )


def restore_role_sql(schema):
    # Cluster roles and their memberships are not included in pg_dump. These
    # password-free identities exist only inside the disposable restore container.
    statements = [
        "CREATE ROLE ec_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        "CREATE ROLE ec_owner LOGIN SUPERUSER",
        "CREATE ROLE temporal LOGIN",
    ]
    if int(schema) >= OPERATOR_ROLE_SCHEMA:
        for name in ("ec_operator_viewer", "ec_operator_controller", "ec_ingestion_executor"):
            statements.append(
                f"CREATE ROLE {name} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        statements.append(
            "CREATE ROLE ec_operator_aggregate_definer NOLOGIN NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        for name in ("ec_dev_operator", "ec_dev_ingestion"):
            statements.append(
                f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        statements.extend(
            [
                "GRANT ec_operator_viewer TO ec_operator_controller",
                "GRANT ec_operator_controller TO ec_dev_operator",
                "GRANT ec_ingestion_executor TO ec_dev_ingestion",
            ]
        )
    if int(schema) >= MODEL_USAGE_ROLE_SCHEMA:
        statements.append(
            "CREATE ROLE ec_model_usage_definer NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
    return "; ".join(statements) + ";"


def verify_operator_roles(sql, schema):
    if int(schema) >= OPERATOR_ROLE_SCHEMA:
        assert (
            sql(
                "SELECT count(*) FROM pg_roles WHERE rolname IN "
                "('ec_operator_viewer','ec_operator_controller','ec_ingestion_executor',"
                "'ec_operator_aggregate_definer','ec_dev_operator','ec_dev_ingestion') "
                "AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication "
                "OR pg_has_role('ec_app',oid,'MEMBER') OR pg_has_role(oid,'ec_app','MEMBER'))"
            )
            == "0"
        ), "Restored operator roles bypass consumer isolation"


def verify_model_usage_role(sql, schema):
    if int(schema) >= MODEL_USAGE_ROLE_SCHEMA:
        assert (
            sql(
                "SELECT count(*) FROM pg_roles r WHERE rolname='ec_model_usage_definer' "
                "AND NOT (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole "
                "OR rolcreatedb OR rolreplication) "
                "AND NOT EXISTS (SELECT 1 FROM pg_auth_members m "
                "WHERE m.member=r.oid OR m.roleid=r.oid)"
            )
            == "1"
        ), "Restored model usage definer is missing or has unsafe privileges/memberships"


def verify_restored_roles(sql, schema):
    verify_operator_roles(sql, schema)
    verify_model_usage_role(sql, schema)


def verify(uri):
    if not uri.startswith(BACKUP_ROOT) or ".." in uri:
        raise SystemExit("Use a development backup prefix")
    with tempfile.TemporaryDirectory(prefix="ec-dev-restore-") as tmp:
        os.chmod(tmp, 0o700)
        folder = pathlib.Path(tmp)
        gc("cp", uri + "/COMPLETE", tmp, stdout=subprocess.DEVNULL)
        gc("cp", uri + "/manifest.json", tmp, stdout=subprocess.DEVNULL)
        manifest = json.loads((folder / "manifest.json").read_text())
        for name, info in manifest["databases"].items():
            if pathlib.Path(name).name != name:
                raise SystemExit("Invalid manifest path")
            gc("cp", uri + "/" + name, tmp, stdout=subprocess.DEVNULL)
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != info["sha256"]:
                raise SystemExit("Backup checksum mismatch")
            container = "ec-restore-" + uuid.uuid4().hex[:10]
            try:
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        container,
                        "--network=none",
                        "-e",
                        "POSTGRES_HOST_AUTH_METHOD=trust",
                        "pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b",
                    ],
                    stdout=subprocess.DEVNULL,
                )

                for _attempt in range(60):
                    ready = subprocess.run(
                        # The image's temporary initialization server listens only
                        # on its Unix socket; TCP waits for the final server.
                        [
                            "docker",
                            "exec",
                            container,
                            "pg_isready",
                            "-h",
                            "127.0.0.1",
                            "-U",
                            "postgres",
                        ],
                        check=False,
                        capture_output=True,
                    )
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Isolated restore database did not become ready")
                run(
                    [
                        "docker",
                        "exec",
                        container,
                        "psql",
                        "-U",
                        "postgres",
                        "-c",
                        restore_role_sql(manifest["schema"]),
                    ],
                    stdout=subprocess.DEVNULL,
                )
                run(["docker", "exec", container, "createdb", "-U", "postgres", "restore"])
                with (folder / name).open("rb") as f:
                    run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            container,
                            "pg_restore",
                            "--exit-on-error",
                            "-U",
                            "postgres",
                            "-d",
                            "restore",
                        ],
                        stdin=f,
                    )
                if info["store"] == "application":

                    def sql(query, user="postgres", restore_container=container):
                        return (
                            run(
                                [
                                    "docker",
                                    "exec",
                                    restore_container,
                                    "psql",
                                    "-U",
                                    user,
                                    "-d",
                                    "restore",
                                    "-At",
                                    "-v",
                                    "ON_ERROR_STOP=1",
                                    "-c",
                                    query,
                                ],
                                capture_output=True,
                            )
                            .stdout.decode()
                            .strip()
                        )

                    assert json.loads(sql(SNAPSHOT_SQL)) == manifest["application_snapshot"], (
                        "Restored aggregates/schema differ"
                    )
                    assert (
                        sql(
                            "SELECT count(*) FROM pg_roles WHERE rolname='ec_app' AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)"
                        )
                        == "0"
                    )
                    verify_restored_roles(sql, manifest["schema"])
                    assert (
                        sql(
                            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relrowsecurity AND NOT c.relforcerowsecurity"
                        )
                        == "0"
                    )
                    ta, tb, ra, rb = [str(uuid.uuid4()) for _ in range(4)]
                    sql(
                        f"INSERT INTO tenants(tenant_id,oidc_subject,notify_email,relay_inbox) VALUES ('{ta}','{ta}','a@example.invalid','{ta}@relay.invalid'),('{tb}','{tb}','b@example.invalid','{tb}@relay.invalid'); INSERT INTO event_requests(request_id,tenant_id,raw_text,constraints) VALUES ('{ra}','{ta}','restore fixture','{{}}'),('{rb}','{tb}','restore fixture','{{}}');"
                    )
                    visible = sql(
                        f"BEGIN; SELECT set_config('app.tenant_id','{ta}',true); SELECT count(*) FROM event_requests WHERE request_id IN ('{ra}','{rb}'); ROLLBACK;",
                        "ec_app",
                    ).splitlines()
                    assert visible[-2] == "1", "Restored application-role isolation failed"
                    print("Application schema, aggregates, grants and tenant isolation passed")
                print("Restored and checksum-verified:", name)
            finally:
                run(["docker", "rm", "-fv", container], stdout=subprocess.DEVNULL)
        payload_dir = folder / "payloads"
        gc("rsync", "--recursive", uri + "/payloads", str(payload_dir), stdout=subprocess.DEVNULL)
        payloads = list(payload_dir.rglob("*.payload"))
        for payload in payloads:
            if hashlib.sha256(payload.read_bytes()).hexdigest() != payload.stem:
                raise SystemExit("Restored payload checksum mismatch")
        print("Restored payload checksums passed:", len(payloads))
        verification = {
            "verified_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "databases": list(manifest["databases"]),
            "payload_count": len(payloads),
            "object_inventory_verified": _verify_object_inventory(
                payload_dir, manifest.get("objects")
            ),
            "application_schema": manifest["schema"],
            "tenant_isolation": "passed",
        }
        (folder / "VERIFIED.json").write_text(json.dumps(verification, indent=2))
        gc("cp", str(folder / "VERIFIED.json"), uri + "/VERIFIED.json", stdout=subprocess.DEVNULL)
    print("Isolated database and payload restore passed")


if __name__ == "__main__":
    # The system Python on macOS may be 3.9 even though the project requires 3.12.
    if sys.version_info < (3, 12):  # noqa: UP036
        raise SystemExit(
            "Python 3.12+ is required; use .venv/bin/python scripts/development/backup.py"
        )
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["backup", "verify", "resume"])
    p.add_argument("uri", nargs="?")
    p.add_argument("--target", choices=TARGETS, default="shared")
    p.add_argument("--profile", choices=("development", "private"), default=None)
    p.add_argument(
        "--hold-stopped",
        action="store_true",
        help="After a successful backup, leave application writers and Temporal stopped for cutover",
    )
    a = p.parse_args()
    select_target(a.target)
    if a.action == "backup":
        if a.uri:
            p.error("backup creates its own prefix; do not supply a URI")
        backup(hold_stopped=a.hold_stopped, profile=a.profile or "development")
    elif a.uri:
        if a.hold_stopped:
            p.error("--hold-stopped applies only to backup")
        if a.profile:
            p.error("--profile applies only to backup; resume uses the saved recovery profile")
        if a.action == "verify":
            verify(a.uri)
        else:
            resume(a.uri)
    else:
        p.error(a.action + " needs a backup gs:// prefix")
