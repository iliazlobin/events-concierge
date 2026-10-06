#!/usr/bin/env python3
"""Put the existing supervised Mac consumer/admin forwards behind port 14001."""

import argparse
import copy
import http.client
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PREFIX = "com.iliazlobin.events-concierge."
CONTEXT = "gke_iz27-platform-dev_us-west1-a_platform-dev"
NAMESPACE = "events-concierge-dev"
JOB_NOT_FOUND = 113
SOURCE = Path(__file__).resolve().parents[2] / "deploy/local-access.Caddyfile"


def forward(data, role, ports):
    """Reject other targets, wildcard binds and hand-edited command shapes."""
    target = (
        "service/events-concierge-frontend"
        if role == "web"
        else "deployment/events-concierge-admin"
    )
    args = data.get("ProgramArguments", [])
    expected = [
        f"--context={CONTEXT}",
        "-n",
        NAMESPACE,
        "port-forward",
        "--address=127.0.0.1",
        target,
    ]
    if (
        data.get("Label") != PREFIX + role
        or "Program" in data
        or len(args) != len(expected) + 2
        or Path(args[0]).name != "kubectl"
        or args[1:-1] != expected
        or args[-1] not in ports
        or not data.get("EnvironmentVariables", {}).get("KUBECONFIG")
    ):
        raise ValueError(
            f"Unexpected {role} access configuration; preserve it and inspect manually"
        )
    return data


def write(path, contents):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(contents)
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=30, **kwargs)


def stop(label):
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode not in (0, 3):
        raise RuntimeError(f"Could not stop owned access job {label}: exit {result.returncode}")


def start(path):
    command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])


def owned_job(label, path, expected):
    """Check the loaded job as well as its file before stopping a label."""
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        if result.returncode == JOB_NOT_FOUND and "Could not find service" in result.stderr:
            return
        raise RuntimeError(f"Cannot verify loaded access job {label}")
    arguments = re.search(r"arguments = \{\n(.*?)\n\s*\}", result.stdout, re.DOTALL)
    source = re.search(r"^\s*path = (.+)$", result.stdout, re.MULTILINE)
    program = re.search(r"^\s*program = (.+)$", result.stdout, re.MULTILINE)
    actual = [line.strip() for line in arguments[1].splitlines()] if arguments else []
    if (
        expected is None
        or source is None
        or program is None
        or source[1] != str(path)
        or actual != expected["ProgramArguments"]
        or program[1] != expected["ProgramArguments"][0]
    ):
        raise ValueError(f"Unrecognized loaded job {label}; preserve it")


def same_frontends(web):
    env = dict(os.environ, **web["EnvironmentVariables"])
    args = [
        *web["ProgramArguments"][:4],
        "--request-timeout=15s",
        "get",
        "deployments",
        "events-concierge-frontend",
        "events-concierge-admin",
        "-o",
        "json",
    ]
    deployments = json.loads(command(args, env=env, capture_output=True, text=True).stdout)["items"]
    images = [
        next(
            c["image"]
            for c in d["spec"]["template"]["spec"]["containers"]
            if c["name"] == "frontend"
        )
        for d in deployments
    ]
    if (
        len(images) != len(("consumer", "admin"))
        or images[0] != images[1]
        or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", images[0])
    ):
        raise ValueError("Consumer/admin frontend digests must match to share /_next assets")


def ready():
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            for path in ("/", "/admin"):
                connection = http.client.HTTPConnection("127.0.0.1", 14001, timeout=3)
                try:
                    connection.request("GET", path)
                    status = connection.getresponse().status
                finally:
                    connection.close()
                if status not in (200, 302, 303, 307, 308, 401, 403):
                    raise ConnectionError(f"{path}: HTTP {status}")
            return
        except (OSError, http.client.HTTPException):
            time.sleep(0.5)
    raise RuntimeError("Combined consumer/admin access did not become ready")


class Access:
    def __init__(self):
        home = Path.home()
        agents = home / "Library/LaunchAgents"
        self.runtime = home / "Library/Application Support/EventsConciergeAccess"
        self.web_path = agents / (PREFIX + "web.plist")
        self.gateway_path = agents / (PREFIX + "gateway.plist")
        self.backup = self.runtime / "web-before-single-port.plist"
        self.config = self.runtime / "local-access.Caddyfile"
        admin_path = agents / (PREFIX + "admin.plist")
        if not self.runtime.is_dir() or self.runtime.is_symlink() or agents.is_symlink():
            raise ValueError("Existing supervised access directory is required")
        for path in (self.web_path, admin_path, self.gateway_path, self.backup, self.config):
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path}")
        self.web = forward(
            plistlib.loads(self.web_path.read_bytes()), "web", {"14001:80", "14011:80"}
        )
        forward(plistlib.loads(admin_path.read_bytes()), "admin", {"14002:3000"})
        self.caddy = shutil.which("caddy")
        if not self.caddy:
            raise RuntimeError("Install Caddy before using this helper")
        self.gateway = {
            "Label": PREFIX + "gateway",
            "ProgramArguments": [
                self.caddy,
                "run",
                "--config",
                str(self.config),
                "--adapter",
                "caddyfile",
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "WorkingDirectory": str(self.runtime),
            "StandardOutPath": str(self.runtime / "gateway.log"),
            "StandardErrorPath": str(self.runtime / "gateway.log"),
            "EnvironmentVariables": {
                "HOME": str(home),
                "PATH": str(Path(self.caddy).parent) + ":/usr/bin:/bin",
            },
        }
        self.verify_files()
        self.verify_jobs()

    def verify_files(self, transition=False):
        for path in (self.web_path, self.gateway_path, self.backup, self.config):
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path}")
        expected = [self.web]
        if transition:
            moved = copy.deepcopy(self.web)
            moved["ProgramArguments"][-1] = "14011:80"
            expected.append(moved)
        if plistlib.loads(self.web_path.read_bytes()) not in expected:
            raise ValueError("The web LaunchAgent changed during preparation; preserve it")
        if (
            self.gateway_path.exists()
            and plistlib.loads(self.gateway_path.read_bytes()) != self.gateway
        ):
            raise ValueError("An unrecognized gateway LaunchAgent exists; preserve it")
        if self.config.exists() and self.config.read_bytes() != SOURCE.read_bytes():
            raise ValueError("An unrecognized local gateway configuration exists; preserve it")

    def verify_jobs(self):
        owned_job(PREFIX + "web", self.web_path, plistlib.loads(self.web_path.read_bytes()))
        owned_job(
            PREFIX + "gateway",
            self.gateway_path,
            self.gateway if self.gateway_path.exists() else None,
        )

    def preview(self):
        print("14001: /admin and /admin/* → admin 14002; other paths → consumer 14011")
        print("Loopback only. Existing IAP, credentials and GKE workloads remain unchanged.")

    def original(self):
        return forward(plistlib.loads(self.backup.read_bytes()), "web", {"14001:80"})

    def restore(self):
        self.verify_files(transition=True)
        self.verify_jobs()
        stop(PREFIX + "gateway")
        stop(PREFIX + "web")
        write(self.web_path, plistlib.dumps(self.original()))
        start(self.web_path)
        self.gateway_path.unlink(missing_ok=True)
        if self.config.exists() and self.config.read_bytes() == SOURCE.read_bytes():
            self.config.unlink()

    def rollback(self):
        self.original()
        self.restore()
        print("Restored consumer 14001 and admin 14002; original access backup retained.")

    def install(self):
        command([self.caddy, "validate", "--config", str(SOURCE), "--adapter", "caddyfile"])
        same_frontends(self.web)
        if not self.backup.exists():
            forward(self.web, "web", {"14001:80"})
            write(self.backup, self.web_path.read_bytes())
        self.original()
        if self.web["ProgramArguments"][-1] == "14001:80":
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 14011))
        self.verify_files()
        self.verify_jobs()
        try:
            stop(PREFIX + "gateway")
            stop(PREFIX + "web")
            updated = copy.deepcopy(self.web)
            updated["ProgramArguments"][-1] = "14011:80"
            write(self.web_path, plistlib.dumps(updated))
            write(self.config, SOURCE.read_bytes())
            write(self.gateway_path, plistlib.dumps(self.gateway))
            start(self.web_path)
            start(self.gateway_path)
            ready()
        except BaseException:
            self.restore()
            raise
        print("Consumer: http://127.0.0.1:14001/ — Admin: http://127.0.0.1:14001/admin")
        print(f"Rollback: python3 {Path(__file__).resolve()} rollback")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "install", "rollback"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This helper manages existing macOS LaunchAgents only")
    os.umask(0o077)
    getattr(Access(), args.action)()


if __name__ == "__main__":
    main()
