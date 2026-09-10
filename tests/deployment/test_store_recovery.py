"""Offline guard and cleanup contracts; these tests never contact Kubernetes."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "store_recovery", ROOT / "scripts/development/check_store_recovery.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def cluster(monkeypatch):
    claims = {
        "items": [
            {
                "metadata": {"name": name},
                "spec": {"volumeName": "pv-" + name},
                "status": {"phase": "Bound"},
            }
            for name in recovery.STORE_CLAIMS.values()
        ]
    }
    pods = {
        selector: {
            "metadata": {"name": selector.split("=")[1] + "-0", "uid": "before-" + selector},
            "spec": {
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}],
                "containers": [
                    {
                        "volumeMounts": [
                            {
                                "name": "data",
                                "mountPath": "/data"
                                if claim == "ec-dev-redis"
                                else "/var/lib/postgresql/data",
                            }
                        ]
                    }
                ],
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for selector, claim in recovery.STORE_CLAIMS.items()
    }
    calls = []
    sql_calls = []

    def obj(*args):
        if args == ("get", "pvc"):
            return copy.deepcopy(claims)
        return {"items": [copy.deepcopy(pods[args[-1]])]}

    def k(*args, **kwargs):
        calls.append(args)
        if args[0] == "delete":
            pod = next(p for p in pods.values() if p["metadata"]["name"] == args[2])
            pod["metadata"]["uid"] = "after-" + pod["metadata"]["uid"]
        if "CONFIG" in args:
            return SimpleNamespace(stdout="appendonly\nyes\n")
        if "SET" in args:
            return SimpleNamespace(stdout="OK\n")
        return SimpleNamespace(stdout="testmarker\n")

    def sql(pod, user, database, statement):
        sql_calls.append((database, statement))
        return "testmarker" if statement.startswith("SELECT") else ""

    monkeypatch.setattr(recovery, "obj", obj)
    monkeypatch.setattr(recovery, "k", k)
    monkeypatch.setattr(recovery, "sql", sql)
    monkeypatch.setattr(recovery.subprocess, "check_output", lambda *a, **kw: recovery.CONTEXT)
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(recovery.uuid, "uuid4", lambda: SimpleNamespace(hex="testmarker"))
    return SimpleNamespace(claims=claims, pods=pods, calls=calls, sql_calls=sql_calls, k=k, sql=sql)


def test_kubectl_commands_pin_context_and_namespace(monkeypatch):
    run = Mock()
    monkeypatch.setattr(recovery.subprocess, "run", run)
    recovery.k("get", "pvc")
    run.assert_called_once_with(
        [recovery.K, "--context", recovery.CONTEXT, "-n", recovery.N, "get", "pvc"],
        check=True,
        timeout=60,
    )


def test_wrong_context_stops_before_cluster_access(cluster, monkeypatch):
    monkeypatch.setattr(recovery.subprocess, "check_output", lambda *a, **kw: "other-cluster")
    with pytest.raises(SystemExit, match="Wrong cluster context"):
        recovery.main()
    assert cluster.calls == []
    assert cluster.sql_calls == []


@pytest.mark.parametrize("problem", ["unbound", "ephemeral", "not_ready", "aof_off"])
def test_preflight_stops_before_writes_or_pod_deletion(cluster, monkeypatch, problem):
    redis = cluster.pods["ec-store=redis"]
    if problem == "unbound":
        cluster.claims["items"][-1]["status"]["phase"] = "Pending"
    elif problem == "ephemeral":
        redis["spec"]["volumes"] = [{"name": "data", "emptyDir": {}}]
    elif problem == "not_ready":
        redis["status"]["conditions"][0]["status"] = "False"
    else:
        monkeypatch.setattr(
            recovery, "k", lambda *a, **kw: SimpleNamespace(stdout="appendonly\nno\n")
        )
    with pytest.raises(RuntimeError):
        recovery.main()
    assert cluster.sql_calls == []
    assert not any(call[0] == "delete" or "SET" in call for call in cluster.calls)


def test_success_checks_replacement_and_cleans_all_markers(cluster):
    recovery.main()
    assert len([call for call in cluster.calls if call[0] == "delete"]) == 3
    assert len([call for call in cluster.sql_calls if call[1].startswith("DROP")]) == 3
    assert any("DEL" in call for call in cluster.calls)


def test_redis_write_error_prevents_pod_deletion(cluster, monkeypatch):
    def k(*args, **kwargs):
        if "SET" in args:
            return SimpleNamespace(stdout="ERR write failed\n")
        return cluster.k(*args, **kwargs)

    monkeypatch.setattr(recovery, "k", k)
    with pytest.raises(RuntimeError, match="Redis recovery marker could not be written"):
        recovery.main()
    assert not any(call[0] == "delete" for call in cluster.calls)
    assert len([call for call in cluster.sql_calls if call[1].startswith("DROP")]) == 3


def test_primary_failure_survives_cleanup_errors_and_remaining_cleanup_runs(cluster, monkeypatch):
    def sql(pod, user, database, statement):
        cluster.sql_calls.append((database, statement))
        if statement.startswith("SELECT"):
            return "lost-marker"
        if statement.startswith("DROP") and database == "events":
            raise OSError("cleanup failed")
        return ""

    monkeypatch.setattr(recovery, "sql", sql)
    with pytest.raises(RuntimeError, match="Persistent marker was lost: events"):
        recovery.main()
    assert len([call for call in cluster.sql_calls if call[1].startswith("DROP")]) == 3
    assert any("DEL" in call for call in cluster.calls)


def test_cleanup_failure_fails_an_otherwise_successful_drill(cluster, monkeypatch):
    def k(*args, **kwargs):
        if "DEL" in args:
            raise OSError("cleanup failed")
        return cluster.k(*args, **kwargs)

    monkeypatch.setattr(recovery, "k", k)
    with pytest.raises(RuntimeError, match="Recovery marker cleanup failed"):
        recovery.main()
