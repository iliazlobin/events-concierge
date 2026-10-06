"""Exercise access ownership and recovery without touching live Mac jobs."""

from __future__ import annotations

import copy
import importlib.util
import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "local_access", ROOT / "scripts/development/local_access.py"
)
assert SPEC is not None and SPEC.loader is not None
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


def plist(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())


def files(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }


def loaded_job(state, label):
    if state.print_failure:
        code, error = state.print_failure
        return "", error, code
    if label not in state.jobs:
        return "", "Could not find service", access.JOB_NOT_FOUND
    path, data = state.jobs[label]
    arguments = "\n".join("\t\t" + arg for arg in data["ProgramArguments"])
    output = (
        f"\tpath = {path}\n\tprogram = {data['ProgramArguments'][0]}\n"
        f"\targuments = {{\n{arguments}\n\t}}\n"
    )
    return output, "", 0


class FakeMac(SimpleNamespace):
    def run(self, args, **kwargs):
        self.calls.append(list(args))
        output, error, code = "", "", 0
        if args[0] == self.caddy and args[1] == "validate":
            operation = ("validate", "")
        elif args[0] == "/opt/homebrew/bin/kubectl" and "get" in args:
            operation = ("get", "")
            assert kwargs["env"]["KUBECONFIG"] == str(self.credentials)
            output = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": name},
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [{"name": "frontend", "image": image}]
                                    }
                                }
                            },
                        }
                        for name, image in zip(
                            ("events-concierge-frontend", "events-concierge-admin"),
                            self.images,
                            strict=True,
                        )
                    ]
                }
            )
        elif args[:2] == ["launchctl", "print"]:
            label = args[-1].rsplit("/", 1)[-1]
            operation = ("print", label)
            output, error, code = loaded_job(self, label)
        elif args[:2] == ["launchctl", "bootout"]:
            label = args[-1].rsplit("/", 1)[-1]
            operation = ("bootout", label)
            if self.fail != operation:
                code = 0 if self.jobs.pop(label, None) is not None else 3
        elif args[:2] == ["launchctl", "bootstrap"]:
            path = Path(args[-1])
            data = plist(path)
            operation = ("bootstrap", data["Label"])
            if self.fail != operation:
                self.jobs[data["Label"]] = (str(path), copy.deepcopy(data))
        else:
            pytest.fail(f"Unexpected subprocess: {args}")
        if self.fail == operation:
            self.fail = None
            code, error = 1, "fixture operation failed"
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, args, output=output, stderr=error)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr=error)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Model launchctl's loaded jobs independently of its plist files."""
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    runtime = home / "Library/Application Support/EventsConciergeAccess"
    agents.mkdir(parents=True)
    runtime.mkdir(parents=True)
    source = tmp_path / "source.Caddyfile"
    source.write_text("http://127.0.0.1:14001 { respond fixture }\n")
    caddy = "/opt/homebrew/bin/caddy"
    monkeypatch.setattr(access.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(access, "SOURCE", source)
    monkeypatch.setattr(access.shutil, "which", lambda name: caddy if name == "caddy" else None)

    def forward(role, port, target):
        return {
            "Label": access.PREFIX + role,
            "ProgramArguments": [
                "/opt/homebrew/bin/kubectl",
                "--context=" + access.CONTEXT,
                "-n",
                access.NAMESPACE,
                "port-forward",
                "--address=127.0.0.1",
                target,
                port,
            ],
            "EnvironmentVariables": {"KUBECONFIG": str(runtime / "kubeconfig")},
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(runtime / (role + ".log")),
        }

    web_path = agents / (access.PREFIX + "web.plist")
    admin_path = agents / (access.PREFIX + "admin.plist")
    web_path.write_bytes(
        plistlib.dumps(forward("web", "14001:80", "service/events-concierge-frontend"))
    )
    admin_path.write_bytes(
        plistlib.dumps(forward("admin", "14002:3000", "deployment/events-concierge-admin"))
    )
    iap_path = agents / (access.PREFIX + "iap.plist")
    iap_path.write_bytes(b"protected IAP fixture")
    credentials = runtime / "kubeconfig"
    credentials.write_bytes(b"protected credential fixture")
    image = "registry.example/events-concierge-web@sha256:" + "a" * 64
    state = FakeMac(
        home=home,
        runtime=runtime,
        web_path=web_path,
        admin_path=admin_path,
        iap_path=iap_path,
        credentials=credentials,
        source=source,
        caddy=caddy,
        calls=[],
        binds=[],
        jobs={},
        images=[image, image],
        fail=None,
        print_failure=None,
    )
    for path in (web_path, admin_path):
        data = plist(path)
        state.jobs[data["Label"]] = (str(path), copy.deepcopy(data))

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            state.binds.append(address)

    monkeypatch.setattr(access.subprocess, "run", state.run)
    monkeypatch.setattr(access.socket, "socket", Probe)
    monkeypatch.setattr(access, "ready", lambda: None)
    return state


def mutations(mac):
    return [call for call in mac.calls if call[:2] in (["launchctl", "bootout"], ["launchctl", "bootstrap"])]


def test_preview_is_read_only(mac):
    before, jobs = files(mac.home), copy.deepcopy(mac.jobs)
    access.Access().preview()
    assert files(mac.home) == before
    assert mac.jobs == jobs
    assert mutations(mac) == []


def test_install_only_changes_web_port_and_adds_owned_gateway(mac):
    before = files(mac.home)
    admin_job = copy.deepcopy(mac.jobs[access.PREFIX + "admin"])
    original = plist(mac.web_path)
    manager = access.Access()
    manager.install()
    expected = copy.deepcopy(original)
    expected["ProgramArguments"][-1] = "14011:80"
    assert plist(mac.web_path) == expected
    assert manager.backup.read_bytes() == before[str(mac.web_path.relative_to(mac.home))]
    assert plist(manager.gateway_path) == manager.gateway
    assert manager.config.read_bytes() == mac.source.read_bytes()
    assert mac.binds == [("127.0.0.1", 14011)]
    assert mac.jobs[access.PREFIX + "web"][1] == expected
    assert mac.jobs[access.PREFIX + "gateway"][1] == manager.gateway
    assert mac.jobs[access.PREFIX + "admin"] == admin_job
    for path in (mac.admin_path, mac.iap_path, mac.credentials):
        assert path.read_bytes() == before[str(path.relative_to(mac.home))]
    assert manager.backup.stat().st_mode & 0o777 == 0o600


def test_repeat_install_and_rollback_preserve_original_backup(mac):
    original = plist(mac.web_path)
    access.Access().install()
    manager = access.Access()
    backup = manager.backup.read_bytes()
    manager.install()
    assert manager.backup.read_bytes() == backup
    access.Access().rollback()
    assert plist(mac.web_path) == original
    assert mac.jobs[access.PREFIX + "web"][1] == original
    assert access.PREFIX + "gateway" not in mac.jobs
    assert not manager.gateway_path.exists()
    assert not manager.config.exists()
    assert manager.backup.read_bytes() == backup


@pytest.mark.parametrize("failure", ["validate", "digests", "mutable", "malformed"])
def test_candidate_validation_rejects_before_writes_or_stops(mac, failure):
    before = files(mac.home)
    if failure == "validate":
        mac.fail = ("validate", "")
    elif failure == "digests":
        mac.images[1] = "registry.example/events-concierge-web@sha256:" + "b" * 64
    else:
        mac.images = [
            "registry.example/events-concierge-web:latest"
            if failure == "mutable"
            else "registry.example/events-concierge-web@sha256:foo"
        ] * 2
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        access.Access().install()
    assert files(mac.home) == before
    assert mutations(mac) == []


@pytest.mark.parametrize("change", ["label", "program", "context", "namespace", "address", "target", "port", "extra", "credentials"])
def test_foreign_forward_configuration_rejected(mac, change):
    web = plist(mac.web_path)
    if change == "label":
        web["Label"] = "foreign-job"
    elif change == "program":
        web["Program"] = "/tmp/foreign-command"
    elif change == "extra":
        web["ProgramArguments"].append("--pod-running-timeout=1m")
    elif change == "credentials":
        web["EnvironmentVariables"].pop("KUBECONFIG")
    else:
        index, value = {
            "context": (1, "--context=foreign-cluster"),
            "namespace": (3, "foreign-namespace"),
            "address": (5, "--address=0.0.0.0"),
            "target": (6, "service/foreign-service"),
            "port": (7, "14001:8080"),
        }[change]
        web["ProgramArguments"][index] = value
    mac.web_path.write_bytes(plistlib.dumps(web))
    before = files(mac.home)
    with pytest.raises(ValueError, match="Unexpected web"):
        access.Access()
    assert files(mac.home) == before
    assert mutations(mac) == []


@pytest.mark.parametrize("foreign", ["gateway-file", "config", "loaded-gateway", "loaded-web"])
def test_foreign_gateway_or_loaded_job_is_preserved(mac, foreign):
    gateway = mac.web_path.parent / (access.PREFIX + "gateway.plist")
    if foreign == "gateway-file":
        gateway.write_bytes(plistlib.dumps({"Label": access.PREFIX + "gateway"}))
    elif foreign == "config":
        (mac.runtime / "local-access.Caddyfile").write_bytes(b"custom gateway configuration")
    else:
        role = "gateway" if foreign == "loaded-gateway" else "web"
        mac.jobs[access.PREFIX + role] = (
            "/tmp/foreign.plist",
            {"ProgramArguments": ["/tmp/foreign-command"]},
        )
    before, jobs = files(mac.home), copy.deepcopy(mac.jobs)
    with pytest.raises(ValueError, match=r"[Uu]nrecognized"):
        access.Access()
    assert files(mac.home) == before
    assert mac.jobs == jobs
    assert mutations(mac) == []


@pytest.mark.parametrize("failure", [(3, "Could not find service"), (113, "Permission denied")])
def test_unverifiable_loaded_job_is_not_assumed_absent(mac, failure):
    mac.print_failure = failure
    with pytest.raises(RuntimeError, match="Cannot verify"):
        access.Access()
    assert mutations(mac) == []


def test_missing_runtime_directory_rejected_before_writes(mac):
    mac.credentials.unlink()
    mac.runtime.rmdir()
    before = files(mac.home)
    with pytest.raises(ValueError, match="directory"):
        access.Access()
    assert files(mac.home) == before
    assert mutations(mac) == []


@pytest.mark.parametrize("name", ["runtime", "web", "backup", "config", "gateway"])
def test_symlink_destinations_rejected(mac, tmp_path, name):
    paths = {
        "runtime": mac.runtime,
        "web": mac.web_path,
        "backup": mac.runtime / "web-before-single-port.plist",
        "config": mac.runtime / "local-access.Caddyfile",
        "gateway": mac.web_path.parent / (access.PREFIX + "gateway.plist"),
    }
    path = paths[name]
    target = tmp_path / "protected-target"
    if name == "runtime":
        mac.runtime.rename(target)
    else:
        target.write_bytes(b"protected bytes")
        path.unlink(missing_ok=True)
    path.symlink_to(target, target_is_directory=name == "runtime")
    with pytest.raises(ValueError):
        access.Access()
    assert mutations(mac) == []
    assert path.is_symlink()


@pytest.mark.parametrize("failure", ["web-stop", "web-start", "gateway-start", "readiness", "interrupt"])
def test_failed_install_restores_original_web_and_removes_owned_gateway(mac, monkeypatch, failure):
    original = plist(mac.web_path)
    before = files(mac.home)
    if failure in ("readiness", "interrupt"):
        def failed_ready():
            if failure == "interrupt":
                raise KeyboardInterrupt("fixture interrupted operation")
            raise RuntimeError("fixture readiness failure")

        monkeypatch.setattr(access, "ready", failed_ready)
    else:
        role = "web" if failure in ("web-start", "web-stop") else "gateway"
        operation = "bootout" if failure == "web-stop" else "bootstrap"
        mac.fail = (operation, access.PREFIX + role)
    manager = access.Access()
    with pytest.raises((RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt)):
        manager.install()
    assert plist(mac.web_path) == original
    assert mac.jobs[access.PREFIX + "web"][1] == original
    assert access.PREFIX + "gateway" not in mac.jobs
    assert not manager.gateway_path.exists()
    assert not manager.config.exists()
    assert manager.backup.read_bytes() == before[str(mac.web_path.relative_to(mac.home))]
    for path in (mac.admin_path, mac.iap_path, mac.credentials):
        assert path.read_bytes() == before[str(path.relative_to(mac.home))]


@pytest.mark.parametrize("foreign", ["config", "gateway-file"])
def test_ownership_changes_during_validation_are_preserved(mac, monkeypatch, foreign):
    manager = access.Access()
    path = manager.config if foreign == "config" else manager.gateway_path
    contents = (
        b"new custom configuration"
        if foreign == "config"
        else plistlib.dumps({"Label": access.PREFIX + "gateway"})
    )
    original = mac.web_path.read_bytes()

    def changed_during_validation(web):
        path.write_bytes(contents)

    monkeypatch.setattr(access, "same_frontends", changed_during_validation)
    with pytest.raises(ValueError, match=r"[Uu]nrecognized"):
        manager.install()
    assert path.read_bytes() == contents
    assert mac.web_path.read_bytes() == original
    assert mutations(mac) == []


def test_invalid_rollback_backup_never_stops_jobs(mac):
    backup = mac.runtime / "web-before-single-port.plist"
    invalid = plist(mac.web_path)
    invalid["ProgramArguments"][-1] = "14011:80"
    backup.write_bytes(plistlib.dumps(invalid))
    before = files(mac.home)
    with pytest.raises(ValueError, match="Unexpected web"):
        access.Access().rollback()
    assert files(mac.home) == before
    assert mutations(mac) == []
