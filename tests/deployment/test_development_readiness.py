"""The private release gate must not accept an absent or unfinished command executor."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "development_readiness", ROOT / "scripts/development/wait_ready.py"
)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)

EXECUTOR = "events-concierge-ingestion-executor"


def ready_deployments(*, include_deferred=False):
    return [
        {
            "metadata": {"name": name, "generation": 2},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 2,
                "updatedReplicas": 1,
                "availableReplicas": 1,
                "readyReplicas": 1,
            },
        }
        for name in (
            "events-concierge-api",
            "events-concierge-admin",
            "events-concierge-frontend",
            "events-concierge-temporal-transactional",
            "events-concierge-temporal-catalog",
            "events-concierge-request-starter",
            "events-concierge-notifier",
            "events-concierge-account-erasure",
            "events-concierge-change-delivery",
            EXECUTOR,
        )
        if include_deferred or name not in readiness.DEFERRED
    ]


def test_other_ready_deployments_cannot_hide_a_missing_executor():
    items = [d for d in ready_deployments() if d["metadata"]["name"] != EXECUTOR]
    assert readiness.pending_deployments(items) == [EXECUTOR]
    assert readiness.pending_deployments(ready_deployments()) == []


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("spec", "replicas", 0),
        ("status", "updatedReplicas", 0),
        ("status", "availableReplicas", 0),
        ("status", "readyReplicas", 0),
        ("status", "observedGeneration", 1),
    ],
)
def test_executor_must_be_running_the_observed_ready_revision(section, field, value):
    items = ready_deployments()
    executor = next(d for d in items if d["metadata"]["name"] == EXECUTOR)
    executor[section][field] = value
    assert readiness.pending_deployments(items) == [EXECUTOR]


def test_waits_for_executor_and_pins_cluster_on_every_read(monkeypatch, capsys):
    missing = [d for d in ready_deployments() if d["metadata"]["name"] != EXECUTOR]
    snapshots = iter([missing, deepcopy(ready_deployments())])
    reads = []
    sleeps = []

    def output(args, **kwargs):
        if args[1:] == ["config", "current-context"]:
            return readiness.CONTEXT
        reads.append(args)
        return json.dumps({"items": next(snapshots)}).encode()

    monkeypatch.setattr(readiness.subprocess, "check_output", output)
    monkeypatch.setattr(readiness.time, "sleep", sleeps.append)
    monkeypatch.setenv("KUBECTL", "/reviewed/kubectl")
    readiness.main()
    assert (
        reads
        == [
            [
                "/reviewed/kubectl",
                "--context",
                readiness.CONTEXT,
                "-n",
                "events-concierge-dev",
                "get",
                "deployments",
                "-o",
                "json",
            ],
        ]
        * 2
    )
    assert sleeps == [5]
    output = capsys.readouterr().out
    assert "Waiting: " + EXECUTOR in output
    assert "All 6 expected deployments" in output


def test_wrong_context_fails_before_deployment_read(monkeypatch):
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return "other-cluster"

    monkeypatch.setattr(readiness.subprocess, "check_output", output)
    with pytest.raises(SystemExit, match="Wrong cluster context"):
        readiness.main()
    assert len(calls) == 1


def test_shared_discovery_requires_active_catalog_and_no_deferred_deployments():
    items = [d for d in ready_deployments() if d["metadata"]["name"] not in readiness.DEFERRED]
    assert readiness.pending_deployments(items) == []
    assert readiness.pending_deployments(ready_deployments(include_deferred=True)) == sorted(
        readiness.DEFERRED
    )
    missing = [d for d in items if d["metadata"]["name"] != EXECUTOR]
    assert readiness.pending_deployments(missing) == [EXECUTOR]
