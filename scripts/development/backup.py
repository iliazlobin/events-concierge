#!/usr/bin/env python3
"""Manual quiesced development backup. Restores are always into disposable local databases."""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import time
import uuid

NS = "events-concierge-dev"
PROJECT = "project-9c8cce04-f94d-40fc-aa6"
ACCOUNT = "iliazlobin27@gmail.com"
K = os.environ.get("KUBECTL", "kubectl")
CONTEXT = "gke_" + PROJECT + "_us-west1-a_ec-dev"
OPERATOR_ROLE_SCHEMA = 182
SNAPSHOT_SQL = "SELECT json_build_object('tenants',(SELECT count(*) FROM tenants),'requests',(SELECT count(*) FROM event_requests),'schema',(SELECT version_num FROM alembic_version))::text"


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def k(*args, **kw):
    return run([K, "--context=" + CONTEXT, "-n", NS, *args], **kw)


def gc(*args, **kw):
    return run(["gcloud", "storage", *args, "--account=" + ACCOUNT, "--project=" + PROJECT], **kw)


def _quiesce(deployments):
    # Let application shutdown finish while Temporal is still available. PostgreSQL
    # StatefulSets and Redis stay running: restarting ephemeral Redis loses state.
    for temporal in (False, True):
        phase = [
            d
            for d in deployments
            if d["metadata"]["name"].startswith("ec-dev-temporal") == temporal
        ]
        for d in phase:
            k(
                "scale",
                "deployment/" + d["metadata"]["name"],
                "--replicas=0",
                stdout=subprocess.DEVNULL,
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
            )


def _resume(desired):
    failures = []
    # Resume Temporal before processes that connect to it. Try every deployment
    # even if one scale call fails, then report any incomplete recovery.
    for name in sorted(desired, key=lambda n: not n.startswith("ec-dev-temporal")):
        try:
            k(
                "scale",
                "deployment/" + name,
                "--replicas=" + str(desired[name]),
                stdout=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Could not restore every original replica count", failures)


def backup(*, hold_stopped=False):
    context = subprocess.check_output([K, "config", "current-context"], text=True).strip()
    if context != CONTEXT:
        raise SystemExit("Wrong cluster context")
    ident = (
        datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    dest = "gs://iz27-ec-dev-backups/" + ident
    deployments = json.loads(k("get", "deployments", "-o", "json", capture_output=True).stdout)[
        "items"
    ]
    # This namespace is dedicated to development. Redis is a store, not a writer
    # to the PostgreSQL/GCS snapshot, and must not be restarted for this backup.
    desired = {d["metadata"]["name"]: d["spec"]["replicas"] for d in deployments}
    writers = [d for d in deployments if d["metadata"]["name"] != "ec-dev-redis"]
    quiesced = {d["metadata"]["name"]: d["spec"]["replicas"] for d in writers}
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
        try:
            _quiesce(writers)
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
            gc(
                "rsync",
                "--recursive",
                "gs://iz27-ec-dev-payloads",
                dest + "/payloads",
                stdout=subprocess.DEVNULL,
            )
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
        finally:
            if not hold_stopped or not complete:
                _resume(quiesced)
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


def verify(uri):
    if not uri.startswith("gs://iz27-ec-dev-backups/") or ".." in uri:
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
                        ["docker", "exec", container, "pg_isready", "-U", "postgres"],
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
                    verify_operator_roles(sql, manifest["schema"])
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
            "application_schema": manifest["schema"],
            "tenant_isolation": "passed",
        }
        (folder / "VERIFIED.json").write_text(json.dumps(verification, indent=2))
        gc("cp", str(folder / "VERIFIED.json"), uri + "/VERIFIED.json", stdout=subprocess.DEVNULL)
    print("Isolated database and payload restore passed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["backup", "verify"])
    p.add_argument("uri", nargs="?")
    p.add_argument(
        "--hold-stopped",
        action="store_true",
        help="After a successful backup, leave application writers and Temporal stopped for cutover",
    )
    a = p.parse_args()
    if a.action == "backup":
        backup(hold_stopped=a.hold_stopped)
    elif a.uri:
        if a.hold_stopped:
            p.error("--hold-stopped applies only to backup")
        verify(a.uri)
    else:
        p.error("verify needs a backup gs:// prefix")
