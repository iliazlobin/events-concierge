"""Offline contracts for idempotent development credential bootstrap and projection."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "development_secrets", ROOT / "scripts/development/bootstrap_secrets.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class SecretManager:
    def __init__(self):
        self.versions = {}
        self.values = {}
        self.writes = []

    def seed(self, name, value, versions=None):
        self.values[name] = value
        self.versions[name] = versions or [{"name": name + "/versions/1", "state": "ENABLED"}]

    def call(self, *args, input=None, **kwargs):
        assert args[:2] == ("secrets", "versions")
        action = args[2]
        if action == "list":
            assert args[4:] == ("--format=json",)
            return SimpleNamespace(stdout=json.dumps(self.versions.get(args[3], [])).encode())
        if action == "add":
            name = args[3]
            assert args[4:] == ("--data-file=-",)
            assert name not in self.versions, "Bootstrap attempted to rotate a credential"
            self.writes.append(name)
            self.seed(name, input.decode())
            return SimpleNamespace(stdout=b"")
        assert action == "access"
        assert args[3] == "1"
        return SimpleNamespace(stdout=self.values[args[4].removeprefix("--secret=")].encode())


@pytest.fixture
def manager(monkeypatch):
    manager = SecretManager()
    monkeypatch.setattr(bootstrap, "gc", manager.call)
    return manager


def test_creates_dedicated_credentials_and_reuses_every_version(manager, monkeypatch, capsys):
    bootstrap.initialize_credentials()
    initial = dict(manager.values)
    assert len(manager.writes) == 11
    assert manager.values["ec-dev-operator-database-url"] == (
        "postgresql+psycopg://ec_dev_operator:"
        + manager.values["ec-dev-operator-role-password"]
        + "@ec-dev-application-postgres:5432/events"
    )
    assert manager.values["ec-dev-ingestion-executor-database-url"] == (
        "postgresql+psycopg://ec_dev_ingestion:"
        + manager.values["ec-dev-ingestion-executor-role-password"]
        + "@ec-dev-application-postgres:5432/events"
    )
    assert (
        manager.values["ec-dev-operator-role-password"]
        != manager.values["ec-dev-ingestion-executor-role-password"]
    )
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _: pytest.fail("rotated password"))
    bootstrap.initialize_credentials()
    assert manager.values == initial
    assert len(manager.writes) == 11
    output = capsys.readouterr()
    assert output.err == ""
    assert all(value not in output.out for value in manager.values.values())


def test_passwords_are_encoded_when_building_urls(manager):
    password = "reserved@:/?#%+characters"
    manager.seed("ec-dev-operator-role-password", password)
    bootstrap.initialize_credentials()
    assert manager.values["ec-dev-operator-database-url"] == (
        "postgresql+psycopg://ec_dev_operator:"
        + quote(password, safe="")
        + "@ec-dev-application-postgres:5432/events"
    )


@pytest.mark.parametrize(
    "versions",
    [
        [{"name": "secret/versions/1", "state": "DISABLED"}],
        [{"name": "secret/versions/1", "state": "DESTROYED"}],
        [{"name": "secret/versions/2", "state": "ENABLED"}],
        [
            {"name": "secret/versions/1", "state": "ENABLED"},
            {"name": "secret/versions/2", "state": "DISABLED"},
        ],
    ],
)
def test_existing_version_layout_never_causes_rotation(manager, versions):
    manager.seed("ec-dev-operator-role-password", "keep-existing", versions)
    with pytest.raises(SystemExit, match="Secret rotation requires"):
        bootstrap.read_or_create("ec-dev-operator-role-password", lambda: pytest.fail("rotated"))
    assert manager.writes == []
    assert manager.values["ec-dev-operator-role-password"] == "keep-existing"


def test_mismatched_derived_url_is_not_replaced(manager):
    bootstrap.initialize_credentials()
    manager.values["ec-dev-operator-database-url"] = "existing-other-reference"
    manager.writes.clear()
    with pytest.raises(SystemExit, match="Existing secret references differ"):
        bootstrap.initialize_credentials()
    assert manager.writes == []
    assert manager.values["ec-dev-operator-database-url"] == "existing-other-reference"


def test_wrong_context_aborts_before_any_cloud_access(monkeypatch):
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **k: "other-cluster\n")
    monkeypatch.setattr(
        bootstrap, "gc", lambda *a, **k: pytest.fail("cloud access before context check")
    )
    with pytest.raises(SystemExit, match="dedicated development cluster context"):
        bootstrap.initialize_credentials(project_to_kubernetes=True)


def test_projection_pins_context_and_only_projects_store_passwords(manager, monkeypatch, capsys):
    monkeypatch.setenv("KUBECTL", "/reviewed/kubectl")
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **k: bootstrap.CONTEXT)
    projected = []

    def apply(args, **kwargs):
        assert args[:5] == ["/reviewed/kubectl", "--context", bootstrap.CONTEXT, "-n", bootstrap.NS]
        assert args[5:] == ["apply", "--server-side", "--field-manager=ec-dev-secrets", "-f", "-"]
        assert kwargs["stdout"] == bootstrap.subprocess.DEVNULL
        assert kwargs["stderr"] == bootstrap.subprocess.PIPE
        projected.append(json.loads(kwargs["input"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", apply)
    bootstrap.initialize_credentials(project_to_kubernetes=True)
    expected = {
        "ec-dev-application-postgres": "postgres-admin",
        "ec-dev-temporal-postgres": "temporal-postgres-admin",
        "ec-dev-redis": "redis-password",
    }
    assert {document["metadata"]["name"] for document in projected} == set(expected)
    for document in projected:
        name = document["metadata"]["name"]
        assert document["metadata"]["namespace"] == bootstrap.NS
        assert document["data"] == {
            "password": base64.b64encode(
                manager.values["ec-dev-" + expected[name]].encode()
            ).decode()
        }
    output = capsys.readouterr()
    assert output.err == ""
    assert all(value not in output.out for value in manager.values.values())


def test_projection_failure_does_not_print_secret_payload(manager, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **k: bootstrap.CONTEXT)
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr=k["input"]),
    )
    with pytest.raises(SystemExit, match="projection failed; values not displayed") as error:
        bootstrap.initialize_credentials(project_to_kubernetes=True)
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert all(value not in str(error.value) for value in manager.values.values())


def test_gcloud_always_pins_account_and_project(monkeypatch):
    seen = []
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda args, **kwargs: seen.append((args, kwargs))
    )
    bootstrap.gc("secrets", "versions", "list", "ec-dev-operator-database-url")
    args, kwargs = seen[0]
    assert args[-3:] == [
        "--account=" + bootstrap.ACCOUNT,
        "--project=" + bootstrap.PROJECT,
        "--quiet",
    ]
    assert kwargs["capture_output"] is True
