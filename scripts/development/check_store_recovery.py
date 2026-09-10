"""Development-only pod replacement drill with persistent markers in all stores."""

import json
import os
import subprocess
import sys
import time
import uuid

K = os.environ.get("KUBECTL", "kubectl")
N = "events-concierge-dev"
CONTEXT = "gke_project-9c8cce04-f94d-40fc-aa6_us-west1-a_ec-dev"
STORE_CLAIMS = {
    "ec-store=application-postgres": "ec-dev-application",
    "ec-store=temporal-postgres": "ec-dev-temporal",
    "ec-store=redis": "ec-dev-redis",
}


def k(*args, **kwargs):
    return subprocess.run(
        [K, "--context", CONTEXT, "-n", N, *args], check=True, timeout=60, **kwargs
    )


def obj(*args):
    return json.loads(k(*args, "-o", "json", capture_output=True).stdout)


def sql(pod, user, database, statement):
    return k(
        "exec",
        "-i",
        pod,
        "--",
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        input=statement,
        text=True,
        capture_output=True,
    ).stdout.strip()


def ready(pod):
    return any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def verify_mount(pod, claim):
    data_path = "/data" if claim == "ec-dev-redis" else "/var/lib/postgresql/data"
    volumes = {
        volume["name"]
        for volume in pod.get("spec", {}).get("volumes", [])
        if volume.get("persistentVolumeClaim", {}).get("claimName") == claim
    }
    if not volumes or not any(
        mount["name"] in volumes and mount.get("mountPath") == data_path
        for container in pod.get("spec", {}).get("containers", [])
        for mount in container.get("volumeMounts", [])
    ):
        raise RuntimeError("Store pod must mount its expected persistent claim: " + claim)


def claim_bindings():
    claims = {item["metadata"]["name"]: item for item in obj("get", "pvc")["items"]}
    result = {}
    for name in STORE_CLAIMS.values():
        claim = claims.get(name, {})
        volume = claim.get("spec", {}).get("volumeName")
        if claim.get("status", {}).get("phase") != "Bound" or not volume:
            raise RuntimeError("Expected store claim must be bound before recovery drill: " + name)
        result[name] = volume
    return result


def preflight():
    bindings = claim_bindings()
    for selector, claim in STORE_CLAIMS.items():
        pods = obj("get", "pod", "-l", selector)["items"]
        if len(pods) != 1 or not ready(pods[0]):
            raise RuntimeError("Recovery drill requires one ready store pod: " + selector)
        verify_mount(pods[0], claim)
    persistence = (
        k(
            "exec",
            "deployment/ec-dev-redis",
            "--",
            "redis-cli",
            "--raw",
            "CONFIG",
            "GET",
            "appendonly",
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    if persistence != ["appendonly", "yes"]:
        raise RuntimeError("Redis AOF persistence must be enabled before recovery drill")
    return bindings


def replace_store_pods():
    for selector, claim in STORE_CLAIMS.items():
        pods = obj("get", "pod", "-l", selector)["items"]
        if len(pods) != 1 or not ready(pods[0]):
            raise RuntimeError("Store changed before replacement: " + selector)
        old = pods[0]
        verify_mount(old, claim)
        started = time.monotonic()
        k(
            "delete",
            "pod",
            old["metadata"]["name"],
            "--wait=true",
            "--timeout=45s",
            stdout=subprocess.DEVNULL,
        )
        for _ in range(120):
            pods = obj("get", "pod", "-l", selector)["items"]
            if (
                len(pods) == 1
                and pods[0]["metadata"]["uid"] != old["metadata"]["uid"]
                and ready(pods[0])
            ):
                verify_mount(pods[0], claim)
                break
            time.sleep(3)
        else:
            raise RuntimeError("Replacement did not become ready: " + selector)
        print(f"{selector}: replacement ready in {time.monotonic() - started:.0f}s", flush=True)


def cleanup_markers(created, redis_created, table, primary_failure):
    cleanup_errors = []
    for pod, user, database in created:
        try:
            sql(pod, user, database, f"DROP TABLE IF EXISTS public.{table}")
        except Exception as error:
            cleanup_errors.append(error)
    if redis_created:
        try:
            k(
                "exec",
                "deployment/ec-dev-redis",
                "--",
                "redis-cli",
                "DEL",
                table,
                stdout=subprocess.DEVNULL,
            )
        except Exception as error:
            cleanup_errors.append(error)
    if cleanup_errors:
        if primary_failure is None:
            raise RuntimeError("Recovery marker cleanup failed") from cleanup_errors[0]
        print(
            "Recovery marker cleanup also failed; inspect probe tables/Redis key",
            file=sys.stderr,
        )


def main():
    context = subprocess.check_output(
        [K, "config", "current-context"], text=True, timeout=10
    ).strip()
    if context != CONTEXT:
        raise SystemExit("Wrong cluster context")
    claims_before = preflight()
    marker = uuid.uuid4().hex
    table = "ec_recovery_probe_" + marker
    stores = [
        ("ec-dev-application-postgres-0", "ec_owner", "events"),
        ("ec-dev-temporal-postgres-0", "temporal", "temporal"),
        ("ec-dev-temporal-postgres-0", "temporal", "temporal_visibility"),
    ]
    created = []
    redis_created = False
    try:
        for pod, user, database in stores:
            created.append((pod, user, database))
            sql(
                pod,
                user,
                database,
                f"CREATE TABLE public.{table}(value text); INSERT INTO public.{table} VALUES ('{marker}');",
            )
        redis_created = True
        result = k(
            "exec",
            "deployment/ec-dev-redis",
            "--",
            "redis-cli",
            "SET",
            table,
            marker,
            "EX",
            "3600",
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != "OK":
            raise RuntimeError("Redis recovery marker could not be written")
        # Allow the configured every-second AOF fsync to finish before replacement.
        time.sleep(2)
        replace_store_pods()
        for pod, user, database in stores:
            if sql(pod, user, database, f"SELECT value FROM public.{table}") != marker:
                raise RuntimeError("Persistent marker was lost: " + database)
            print(database + ": persistent marker survived", flush=True)
        value = k(
            "exec",
            "deployment/ec-dev-redis",
            "--",
            "redis-cli",
            "--raw",
            "GET",
            table,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if value != marker:
            raise RuntimeError("Redis marker was lost")
        if claims_before != claim_bindings():
            raise RuntimeError("Persistent volume binding changed")
        print("Redis marker survived; all persistent volume bindings unchanged", flush=True)
    finally:
        primary_failure = sys.exc_info()[1]
        cleanup_markers(created, redis_created, table, primary_failure)


if __name__ == "__main__":
    main()
