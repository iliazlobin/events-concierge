"""The private release gate must not accept an absent or unfinished command executor."""

import importlib.util
import json
import runpy
import sys
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
                "replicas": 1,
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
        ("status", "replicas", 2),
        ("status", "unavailableReplicas", 1),
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
            assert kwargs["timeout"] == 10
            return readiness.CONTEXT
        assert kwargs["timeout"] == 30
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
                "--request-timeout=20s",
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


def private_deployments():
    return [d for d in ready_deployments() if d["metadata"]["name"] != "events-concierge-admin"]


def test_private_readiness_retains_five_app_deployments_with_public_sidecar():
    assert readiness.pending_deployments(private_deployments(), profile="private") == []
    assert len(readiness.expected_replicas("private")) == 5


@pytest.mark.parametrize(
    "name", ["events-concierge-admin", "events-concierge-notifier", "unknown-operator"]
)
def test_private_readiness_rejects_unapproved_deployments_even_when_stopped(name):
    items = private_deployments()
    unexpected = deepcopy(items[0])
    unexpected["metadata"]["name"] = name
    unexpected["spec"]["replicas"] = 0
    items.append(unexpected)
    assert readiness.pending_deployments(items, profile="private") == [name]


PRIVATE_OPERATORS = {"events-concierge-operator-api", "events-concierge-operator-frontend"}


def operator_deployments():
    items = private_deployments()
    for name in sorted(PRIVATE_OPERATORS):
        operator = deepcopy(items[0])
        operator["metadata"]["name"] = name
        items.append(operator)
    return items


def test_private_readiness_requires_explicit_operator_inventory_and_both_singletons():
    assert readiness.pending_deployments(operator_deployments(), profile="private") == sorted(
        PRIVATE_OPERATORS
    )
    assert readiness.pending_deployments(
        private_deployments(), profile="private", operator=True
    ) == sorted(PRIVATE_OPERATORS)
    assert (
        readiness.pending_deployments(operator_deployments(), profile="private", operator=True)
        == []
    )
    expected = readiness.expected_replicas("private", operator=True)
    assert {name: expected[name] for name in PRIVATE_OPERATORS} == dict.fromkeys(
        PRIVATE_OPERATORS, 1
    )
    assert len(expected) == 7


@pytest.mark.parametrize("name", sorted(PRIVATE_OPERATORS))
@pytest.mark.parametrize(
    "section,field,value",
    [
        ("spec", "replicas", 0),
        ("spec", "replicas", 2),
        ("status", "replicas", 2),
        ("status", "updatedReplicas", 0),
        ("status", "availableReplicas", 0),
        ("status", "readyReplicas", 0),
        ("status", "observedGeneration", 1),
        ("status", "unavailableReplicas", 1),
    ],
)
def test_operator_readiness_requires_the_exact_observed_singleton(name, section, field, value):
    items = operator_deployments()
    item = next(d for d in items if d["metadata"]["name"] == name)
    item[section][field] = value
    assert readiness.pending_deployments(items, profile="private", operator=True) == [name]


def test_operator_option_cannot_hide_unapproved_private_processes():
    items = operator_deployments()
    demo = deepcopy(items[0])
    demo["metadata"]["name"] = "events-concierge-admin"
    items.append(demo)
    assert readiness.pending_deployments(items, profile="private", operator=True) == [
        "events-concierge-admin"
    ]


def test_wait_ready_operator_flag_waits_for_both_with_unchanged_deployment_inventory(
    monkeypatch, capsys
):
    name = "events-concierge-operator-api"
    ready = operator_deployments()
    missing = [d for d in ready if d["metadata"]["name"] != name]
    snapshots = iter([missing, ready])
    sleeps = []
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        if args[1:] == ["config", "current-context"]:
            return readiness.CONTEXT
        assert readiness.CONTEXT in args
        return json.dumps({"items": next(snapshots)}).encode()

    monkeypatch.setattr(readiness.subprocess, "check_output", output)
    monkeypatch.setattr(readiness.time, "sleep", sleeps.append)
    readiness.main(profile="private", operator=True)
    assert len(calls) == 3
    assert sleeps == [5]
    output = capsys.readouterr().out
    assert "Waiting: " + name in output
    assert "All 7 expected deployments" in output


def test_development_cannot_request_operator_before_any_cluster_read(monkeypatch):
    calls = []
    monkeypatch.setattr(
        readiness.subprocess, "check_output", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(SystemExit, match="private"):
        readiness.main(operator=True)
    assert not calls


def test_readiness_cli_operator_flag_fails_closed_for_default_development(monkeypatch):
    calls = []
    path = ROOT / "scripts/development/wait_ready.py"
    monkeypatch.setattr(sys, "argv", [str(path), "--operator"])
    monkeypatch.setattr(
        readiness.subprocess, "check_output", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(SystemExit, match="private"):
        runpy.run_path(str(path), run_name="__main__")
    assert not calls
