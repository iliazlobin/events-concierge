#!/usr/bin/env python3
"""Credential-free checks for two consecutive disposable Events Concierge CI jobs."""

import argparse
import http
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import urllib.request

CI_UID = 1001


def check_privileges():
    if os.getuid() != CI_UID:
        raise RuntimeError("CI must run as UID 1001.")
    for forbidden in (
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
        "/var/run/docker.sock",
        "/run/containerd/containerd.sock",
        "/var/lib/events-concierge",
        "/var/lib/symphony-auth",
        "/var/lib/symphony",
        str(pathlib.Path.home() / ".codex/auth.json"),
        str(pathlib.Path.home() / ".config/gcloud/application_default_credentials.json"),
    ):
        if pathlib.Path(forbidden).exists():
            raise RuntimeError(f"Unexpected protected path: {forbidden}")
    if any(shutil.which(name) for name in ("sudo", "docker", "kubectl", "gcloud", "helm", "codex")):
        raise RuntimeError("CI image must not contain privilege, cluster or model-control tools.")
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "DOCKER_HOST",
        "GITHUB_APP_PRIVATE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        if os.environ.get(name):
            raise RuntimeError(f"Unexpected privileged configuration: {name}")
    status = pathlib.Path("/proc/self/status").read_text()
    if not re.search(r"^CapEff:\s+0+$", status, re.M) or not re.search(
        r"^NoNewPrivs:\s+1$", status, re.M
    ):
        raise RuntimeError("CI capabilities or privilege escalation are enabled.")
    mounts = [
        line.split() for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines()
    ]
    if not any(fields[4] == "/" and "ro" in fields[5].split(",") for fields in mounts):
        raise RuntimeError("CI root filesystem must be read-only.")
    if pathlib.Path("/proc/1/comm").read_text().strip() != "tini":
        raise RuntimeError("CI must use its init process as PID 1.")


def check_tools(expected):
    if platform.python_version() != expected["python_version"]:
        raise RuntimeError("Unexpected Python version.")
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    if node != "v" + expected["node_version"]:
        raise RuntimeError("Unexpected Node version.")
    uv = subprocess.check_output(["uv", "--version"], text=True).split()[1]
    if uv != expected["uv_version"]:
        raise RuntimeError("Unexpected uv version.")
    version_environment = dict(os.environ)
    version_environment.pop("ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT", None)
    runner = subprocess.check_output(
        ["/home/runner/bin/Runner.Listener", "--version"],
        text=True,
        env=version_environment,
    ).strip()
    if runner != expected["runner_version"]:
        raise RuntimeError("Unexpected GitHub runner version.")
    return node, uv, runner


def check_network():
    for host, port in (("10.48.0.1", 443), ("10.40.0.2", 443), ("169.254.169.254", 80)):
        try:
            connection = socket.create_connection((host, port), timeout=2)
        except OSError:
            continue
        connection.close()
        raise RuntimeError(f"Protected endpoint reachable: {host}:{port}")
    socket.getaddrinfo("api.github.com", 443)
    with urllib.request.urlopen("https://api.github.com/", timeout=15) as response:
        if response.status != http.HTTPStatus.OK:
            raise RuntimeError("GitHub HTTPS access failed.")


def probe(write_sentinel=False):
    check_privileges()
    sentinel = pathlib.Path.home() / ".events-concierge-ci-sentinel"
    if sentinel.exists():
        raise RuntimeError("Previous job data survived runner replacement.")
    expected = json.loads((pathlib.Path(__file__).parent / "versions.json").read_text())
    node, uv, runner = check_tools(expected)
    check_network()
    if write_sentinel:
        sentinel.write_text("This file must not exist in the next runner.\n")
    print(
        json.dumps(
            {
                "runner": os.environ.get("RUNNER_NAME"),
                "revision": os.environ.get("GITHUB_SHA"),
                "uid": os.getuid(),
                "python": platform.python_version(),
                "node": node,
                "uv": uv,
                "runner_version": runner,
                "network_isolation": "passed",
                "previous_job_data": "absent",
                "sentinel_written": write_sentinel,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-sentinel", action="store_true")
    probe(parser.parse_args().write_sentinel)
