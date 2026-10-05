"""Offline public-edge contracts and a disposable, network-isolated proxy rehearsal."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/events-concierge"
HOST = "events.iliazlobin.com"
COMPONENT = "app.kubernetes.io/component"


def render(binary: str, directory: Path, overrides: dict[str, Any]):
    values = directory / "values.json"
    values.write_text(json.dumps(overrides))
    return subprocess.run(
        [
            binary, "template", "events-concierge", str(CHART),
            "--namespace", "events-concierge-staging",
            "-f", str(CHART / "values-staging.example.yaml"),
            "-f", str(CHART / "values-consumer-identity.example.yaml"),
            "-f", str(values),
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.fixture(scope="module")
def helm():
    binary = os.environ.get("EC_HELM_BINARY") or shutil.which("helm")
    if not binary:
        pytest.skip("Deployment-validation CI installs Helm and runs this contract")
    return binary


@pytest.fixture(scope="module")
def public_values():
    return {
        "global": {"runtimeProviderReady": True},
        "publicTunnel": {"enabled": True, "tokenSecretName": "ec-cloudflare-tunnel-v1"},
    }


@pytest.fixture(scope="module")
def resources(helm, public_values, tmp_path_factory):
    result = render(helm, tmp_path_factory.mktemp("public-render"), public_values)
    assert result.returncode == 0, result.stderr
    if output := os.environ.get("EC_DEPLOYMENT_RENDER_DIR"):
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "public-enabled.yaml").write_text(result.stdout)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def named(resources, kind, suffix):
    return next(item for item in resources if item["kind"] == kind
                and item["metadata"]["name"] == f"events-concierge-{suffix}")


def matches(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator == "NotIn" and labels.get(key) in values:
            return False
        if operator == "In" and labels.get(key) not in values:
            return False
    return True


def test_publication_is_opt_in(helm, tmp_path):
    result = render(helm, tmp_path, {"global": {"runtimeProviderReady": True}})
    assert result.returncode == 0, result.stderr
    assert "name: events-concierge-public-tunnel" not in result.stdout


@pytest.mark.parametrize("override", [
    {"global": {"deploymentProfile": "development"}},
    {"global": {"releasePhase": "migration"}},
    {"global": {"runtimeProviderReady": False}},
    {"applicationConfig": {"EC_MOCK_CLOUD": "true"}},
    {"applicationConfig": {"EC_PUBLIC_BASE_URL": "https://other.example.com"}},
    {"applicationConfig": {"EC_IDENTITY_PLATFORM_ENABLED": "false"}},
    {"applicationConfig": {"EC_ADMIN_INGESTION_ENABLED": "true"}},
    {"applicationConfig": {"EC_OIDC_BFF_ENABLED": "true"}},
    {"networkPolicy": {"enabled": False}},
    {"gateway": {"enabled": True}},
    {"publicTunnel": {"tokenSecretName": ""}},
    {"publicTunnel": {"replicas": 1}},
    {"publicTunnel": {"hostname": "*.iliazlobin.com"}},
])
def test_unsafe_publication_fails_before_deployment(helm, public_values, tmp_path, override):
    values = copy.deepcopy(public_values)
    for key, change in override.items():
        values.setdefault(key, {}).update(change)
    result = render(helm, tmp_path, values)
    assert result.returncode != 0, "Unsafe public release unexpectedly rendered"


def test_connector_has_only_its_credential_and_no_exposed_service(resources):
    pod = named(resources, "Deployment", "public-tunnel")["spec"]["template"]["spec"]
    assert not pod["automountServiceAccountToken"]
    assert not pod.get("serviceAccountName")
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["token"]["secret"] == {
        "secretName": "ec-cloudflare-tunnel-v1", "defaultMode": 0o440,
        "items": [{"key": "token", "path": "token"}],
    }
    tunnel, proxy = pod["containers"]
    assert tunnel["name"] == "cloudflared"
    assert "--token-file" in tunnel["args"] and "--token" not in tunnel["args"]
    assert not tunnel.get("envFrom") and not tunnel.get("env")
    assert "token" not in {mount["name"] for mount in proxy["volumeMounts"]}
    for container in pod["containers"]:
        assert re.search(r"@sha256:[a-f0-9]{64}$", container["image"])
        assert container["securityContext"]["readOnlyRootFilesystem"]
        assert not container["securityContext"]["allowPrivilegeEscalation"]
    assert not any(item["kind"] in {"Gateway", "Secret"} for item in resources)
    assert not any(item["kind"] == "Service" and "tunnel" in item["metadata"]["name"]
                   for item in resources)


def test_additive_network_policies_cannot_bypass_connector_isolation(resources):
    labels = named(resources, "Deployment", "public-tunnel")["spec"]["template"]["metadata"]["labels"]
    selected = [item for item in resources if item["kind"] == "NetworkPolicy"
                and matches(item["spec"]["podSelector"], labels)]
    assert {item["metadata"]["name"] for item in selected} == {
        "events-concierge-default-deny", "events-concierge-public-tunnel",
    }
    policy = named(resources, "NetworkPolicy", "public-tunnel")["spec"]
    assert {port["port"] for rule in policy["egress"] for port in rule["ports"]} == {
        53, 3000, 7844,
    }
    internet = policy["egress"][-1]
    assert {peer["ipBlock"]["cidr"] for peer in internet["to"]} == {
        "198.41.192.0/24", "198.41.200.0/24",
    }
    origin = policy["egress"][1]
    assert origin["to"][0]["podSelector"]["matchLabels"][COMPONENT] == "frontend"
    ingress = named(resources, "NetworkPolicy", "frontend-ingress")["spec"]["ingress"]
    assert len(ingress) == 1
    assert ingress[0]["from"][0]["podSelector"]["matchLabels"][COMPONENT] == "public-tunnel"
    assert policy["ingress"][0]["ports"] == [{"port": 2000, "protocol": "TCP"}]


@pytest.fixture(scope="module")
def proxy_rehearsal(resources):
    if os.environ.get("EC_PUBLIC_TUNNEL_DOCKER") != "1":
        pytest.skip("Set EC_PUBLIC_TUNNEL_DOCKER=1 for disposable local/CI edge tests")
    docker = shutil.which("docker")
    assert docker, "The requested edge rehearsal requires Docker"
    pod = named(resources, "Deployment", "public-tunnel")["spec"]["template"]["spec"]
    image = pod["containers"][1]["image"]
    config = named(resources, "ConfigMap", "public-tunnel")["data"]["Caddyfile"]
    config = config.replace("events-concierge-frontend:80", "127.0.0.1:8082")
    config += '''
http://:8082 {
  bind 127.0.0.1
  @private path /admin /admin/*
  respond @private "PRIVATE_FIXTURE" 200
  respond "PUBLIC_FIXTURE host={http.request.host} proto={http.request.header.X-Forwarded-Proto} forwarded={http.request.header.Forwarded} identity={http.request.header.X-Goog-Authenticated-User-Email} access={http.request.header.Cf-Access-Authenticated-User-Email} middleware={http.request.header.X-Middleware-Subrequest}" 200
}
'''
    name = f"ec-public-edge-test-{uuid.uuid4().hex[:12]}"

    def command(*args, data=None, timeout=30):
        return subprocess.run([docker, *args], input=data, capture_output=True, text=True,
                              check=False, timeout=timeout)

    validation = command("run", "--rm", "-i", "--network", "none", "--user", "10001:10001",
                         "--read-only", image, "caddy", "validate", "--config", "/dev/stdin",
                         "--adapter", "caddyfile", data=config)
    assert validation.returncode == 0, validation.stderr
    script = (
        "while [ ! -f /tmp/Caddyfile ]; do sleep 1; done; "
        "exec caddy run --config /tmp/Caddyfile --adapter caddyfile"
    )
    start = command("run", "-d", "--name", name, "--network", "none",
                    "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
                    "--cap-add", "NET_BIND_SERVICE",
                    "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "0.5",
                    "--tmpfs", "/tmp:uid=10001,gid=10001,size=16777216",
                    "-e", "XDG_CONFIG_HOME=/tmp/config", "-e", "XDG_DATA_HOME=/tmp/data",
                    "--entrypoint", "sh", image, "-c", script)
    try:
        assert start.returncode == 0, start.stderr
        injected = command("exec", "-i", name, "sh", "-c",
                           "cat > /tmp/Caddyfile.pending && mv /tmp/Caddyfile.pending /tmp/Caddyfile",
                           data=config)
        assert injected.returncode == 0, injected.stderr
        ready = command("exec", name, "sh", "-c",
                        "for n in 1 2 3 4 5; do wget -q -O /dev/null http://127.0.0.1:8081/readyz && exit 0; sleep 1; done; exit 1")
        if ready.returncode != 0:
            logs = command("logs", name)
            state = command("inspect", "--format", "OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}}", name)
            pytest.fail(ready.stderr + logs.stderr + logs.stdout + state.stdout)

        def request(path, host=HOST, headers=()):
            extra_headers = "".join(f"{key}: {value}\r\n" for key, value in headers)
            # Keep stdin open while both proxy hops respond; BusyBox nc otherwise
            # cancels the request as soon as docker exec delivers input EOF.
            result = command("exec", "-i", name, "sh", "-c",
                             "(cat; sleep 1) | nc -w 5 127.0.0.1 8080",
                             data=f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{extra_headers}Connection: close\r\n\r\n")
            assert result.returncode == 0, result.stderr
            return result.stdout

        yield request
    finally:
        cleanup = command("rm", "-f", name)
        assert cleanup.returncode == 0, cleanup.stderr


@pytest.mark.parametrize("path", ["/", "/sign-in", "/v1/events?city=sanfrancisco"])
def test_consumer_pages_and_api_remain_available(proxy_rehearsal, path):
    response = proxy_rehearsal(path)
    assert "200 OK" in response.splitlines()[0]
    assert "PUBLIC_FIXTURE" in response


def test_forwarded_authority_is_rebuilt_and_operator_headers_are_removed(proxy_rehearsal):
    spoofed = "UNTRUSTED_FIXTURE"
    response = proxy_rehearsal("/", headers=[
        ("Forwarded", spoofed),
        ("X-Forwarded-Host", spoofed),
        ("X-Forwarded-Proto", "http"),
        ("X-Goog-Authenticated-User-Email", spoofed),
        ("Cf-Access-Authenticated-User-Email", spoofed),
        ("X-Middleware-Subrequest", spoofed),
    ])
    assert "200 OK" in response.splitlines()[0]
    assert "host=events.iliazlobin.com proto=https" in response
    assert spoofed not in response


@pytest.mark.parametrize("path", [
    "/admin", "/admin/", "/admin/v1/ingestion", "/%61dmin/",
    "//admin/", "/v1/../admin/", "/v1/%2e%2e/admin/", "/%2fadmin/",
    "/healthz", "/readyz", "/metrics",
])
def test_private_and_probe_paths_never_reach_the_frontend(proxy_rehearsal, path):
    response = proxy_rehearsal(path)
    assert "404 Not Found" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" not in response and "PUBLIC_FIXTURE" not in response


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "hermes.iliazlobin.com", "other.example.com"])
def test_unapproved_host_cannot_become_a_loopback_admin_request(proxy_rehearsal, host):
    response = proxy_rehearsal("/", host)
    assert "404 Not Found" in response.splitlines()[0]
    assert "PUBLIC_FIXTURE" not in response
