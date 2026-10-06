"""Cloudflare operator render/startup and additive network isolation, without provider calls."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from events_concierge.api.operator import create_operator_app
from events_concierge.api.operator_auth import CloudflareAccessOperatorIdentityVerifier
from events_concierge.config import Settings
from events_concierge.deployment.startup import preflight_operator_runtime

SPEC = importlib.util.spec_from_file_location(
    "cloudflare_private_operator_contracts",
    Path(__file__).with_name("test_private_operator_integration.py"),
)
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)
helm, render, documents, resource, pod, environment = (
    operator.helm,
    operator.render,
    operator.documents,
    operator.resource,
    operator.pod,
    operator.environment,
)
TEAM = "https://validation-team.cloudflareaccess.com"
AUD = "a" * 64
SUBJECT = "7335d417-61da-459d-899c-0a01c76a2f94"


def values():
    result = operator.operator_values()
    result["operator"].update(
        {
            "authProvider": "cloudflare_access",
            "hostname": "admin-events.iliazlobin.com",
            "tlsSecretName": "",
            "iapAudience": "",
            "iapClientId": "",
            "iapClientSecretName": "",
            "frontendIngressCidrs": [],
            "cloudflareAccess": {
                "teamDomain": TEAM,
                "audience": AUD,
                "jwksCidrs": ["104.16.0.0/13"],
            },
            "subjectRoles": {SUBJECT: "reviewer"},
        }
    )
    result["publicTunnel"] = {"enabled": True, "tokenSecretName": "validation-events-tunnel"}
    return result


@pytest.fixture(scope="module")
def resources(helm, tmp_path_factory):
    return documents(render(helm, tmp_path_factory.mktemp("cloudflare-admin"), values()))


def test_cloudflare_operator_needs_no_iap_or_public_gcp_edge(resources):
    assert not any(
        kind in {"Gateway", "HTTPRoute", "GCPBackendPolicy", "Secret"} for kind, _ in resources
    )
    config = resource(resources, "ConfigMap", "private-operator-api")["data"]
    assert config["EC_OPERATOR_AUTH_PROVIDER"] == "cloudflare_access"
    assert config["EC_OPERATOR_CLOUDFLARE_TEAM_DOMAIN"] == TEAM
    assert config["EC_OPERATOR_CLOUDFLARE_AUDIENCE"] == AUD
    assert config["EC_OPERATOR_PUBLIC_ORIGIN"] == "https://admin-events.iliazlobin.com"
    assert "EC_OPERATOR_IAP_AUDIENCE" not in config
    front = environment(pod(resources, "operator-frontend")["containers"][0])
    assert front["EC_OPERATOR_AUTH_PROVIDER"] == "cloudflare_access"
    assert front["EC_OPERATOR_PUBLIC_ORIGIN"] == config["EC_OPERATOR_PUBLIC_ORIGIN"]
    assert not any("SECRET" in key or "AUDIENCE" in key or "TEAM_DOMAIN" in key for key in front)


def test_rendered_cloudflare_settings_pass_real_operator_startup(resources):
    config = copy.deepcopy(resource(resources, "ConfigMap", "private-operator-api")["data"])
    del config["EC_OPERATOR_DATABASE_URL_FILE"]
    config["EC_OPERATOR_DATABASE_URL"] = (
        "postgresql+psycopg://ec_dev_operator:synthetic@"
        "ec-dev-application-postgres.events-concierge-dev.svc.cluster.local/events?sslmode=verify-full"
    )
    config.update(environment(pod(resources, "operator-api")["containers"][0]))
    with patch.dict(os.environ, config, clear=True):
        settings = Settings(_env_file=None)
    preflight_operator_runtime(settings)
    app = create_operator_app(settings)
    assert isinstance(
        app.state.operator_identity_verifier, CloudflareAccessOperatorIdentityVerifier
    )
    assert settings.operator_subject_roles == {SUBJECT: "reviewer"}
    assert not settings.identity_platform_enabled


def test_consumer_listener_is_unchanged_and_admin_listener_has_exact_host(
    resources, helm, tmp_path
):
    config = values()
    config["operator"]["enabled"] = False
    for name in ("operator-api", "operator-frontend"):
        config["workloads"][name]["enabled"] = False
    before = documents(render(helm, tmp_path, config))
    consumer = resource(before, "ConfigMap", "public-tunnel")["data"]["Caddyfile"]
    combined = resource(resources, "ConfigMap", "public-tunnel")["data"]["Caddyfile"]
    assert combined.startswith(consumer)
    admin = combined[len(consumer) :]
    assert "http://:8082" in admin and "bind 127.0.0.1" in admin
    assert "host admin-events.iliazlobin.com" in admin
    assert "path /admin /admin/* /_next/* /favicon.ico" in admin
    assert "header Cf-Access-Jwt-Assertion *" in admin
    assert "vars operator_access_assertion {http.request.header.Cf-Access-Jwt-Assertion}" in admin
    assert "request_header -Cf-Access-*" in admin
    assert "header_up Cf-Access-Jwt-Assertion {http.vars.operator_access_assertion}" in admin
    assert "-Cf-Access-*" in consumer and "-X-Goog-*" in consumer
    assert "/admin" not in consumer
    assert "path /healthz" not in admin and "path /readyz" not in admin and "path /v1" not in admin


@pytest.mark.parametrize(
    "path,value",
    [
        (("operator", "authProvider"), "headers"),
        (("operator", "hostname"), "events.iliazlobin.com"),
        (("operator", "hostname"), "*.iliazlobin.com"),
        (("publicTunnel", "enabled"), False),
        (("operator", "subjectRoles"), {}),
        (("operator", "iapAudience"), "existing-iap"),
        (("operator", "tlsSecretName"), "gateway-tls"),
        (("operator", "frontendIngressCidrs"), ["0.0.0.0/0"]),
        (("operator", "subjectRoles"), {"accounts.google.com:owner": "reviewer"}),
        (("operator", "cloudflareAccess", "teamDomain"), TEAM + ".attacker.test"),
        (("operator", "cloudflareAccess", "teamDomain"), TEAM + "/"),
        (("operator", "cloudflareAccess", "audience"), "consumer-client"),
        (("operator", "cloudflareAccess", "jwksCidrs"), []),
        (("operator", "cloudflareAccess", "jwksCidrs"), ["0.0.0.0/0"]),
        (("operator", "cloudflareAccess", "jwksCidrs"), ["10.0.0.0/8"]),
    ],
)
def test_unsafe_cloudflare_operator_profile_cannot_render(helm, tmp_path, path, value):
    config = copy.deepcopy(values())
    target = config
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    result = render(helm, tmp_path, config)
    assert result.returncode != 0, f"unsafe Cloudflare configuration rendered: {path}"


@pytest.fixture(scope="module")
def combined(resources, helm):
    data = subprocess.run(
        [
            helm,
            "template",
            "ec-dev-stores",
            str(operator.ROOT / "deploy/helm/events-concierge-dev-data"),
            "-n",
            "events-concierge-dev",
            "-f",
            str(
                operator.ROOT
                / "deploy/helm/events-concierge-dev-data/values-shared-development.yaml"
            ),
            "-f",
            str(operator.ROOT / "deploy/helm/events-concierge-dev-data/values-private-tls.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    stores = documents(data)
    assert not (resources.keys() & stores.keys())
    return resources | stores


def test_additive_policies_allow_only_connector_frontend_and_fixed_jwks(combined):
    policies = [item for (kind, _), item in combined.items() if kind == "NetworkPolicy"]
    front, api, connector, consumer = (
        operator.labels(combined, suffix)
        for suffix in ("operator-frontend", "operator-api", "public-tunnel", "frontend")
    )
    allows = operator._allows
    assert allows(policies, connector, "egress", 3000, peer_labels=front)
    assert allows(policies, front, "ingress", 3000, peer_labels=connector)
    assert allows(policies, connector, "egress", 3000, peer_labels=consumer)
    assert allows(policies, api, "ingress", 8000, peer_labels=front)
    assert allows(policies, api, "egress", 443, address="104.19.194.29")
    for address in ("203.0.113.10", "169.254.169.254", "10.40.0.10"):
        assert not allows(policies, api, "egress", 443, address=address)
    for address in ("130.211.0.1", "35.191.0.1", "203.0.113.10"):
        assert not allows(policies, front, "ingress", 3000, address=address)
    assert not allows(policies, api, "ingress", 8000, peer_labels=connector)
    assert not allows(policies, connector, "egress", 8000, peer_labels=api)
    assert not allows(policies, front, "ingress", 3000, peer_labels=consumer)


@pytest.fixture
def native_proxy(resources, tmp_path):
    """Run the rendered filter with disposable loopback listeners, without Docker."""
    caddy = os.environ.get("EC_CADDY_BINARY") or shutil.which("caddy")
    if not caddy:
        pytest.skip("native Caddy is unavailable; container proxy checks run in CI")

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(
                {
                    "headers": dict(self.headers),
                    "path": self.path,
                    "backend": self.server.server_port,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    consumer = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    admin = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    servers = [consumer, admin]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    sockets = [socket.socket() for _ in range(3)]
    for listener in sockets:
        listener.bind(("127.0.0.1", 0))
    consumer_port, probe_port, admin_port = [listener.getsockname()[1] for listener in sockets]
    config = resource(resources, "ConfigMap", "public-tunnel")["data"]["Caddyfile"]
    for old, new in ((8080, consumer_port), (8081, probe_port), (8082, admin_port)):
        config = config.replace(f"http://:{old}", f"http://:{new}")
    config = config.replace(
        "events-concierge-operator-frontend:80", f"127.0.0.1:{admin.server_port}"
    )
    config = config.replace("events-concierge-frontend:80", f"127.0.0.1:{consumer.server_port}")
    path = tmp_path / "Caddyfile"
    path.write_text(config)
    runtime_env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path))
    validated = subprocess.run(
        [caddy, "validate", "--config", str(path), "--adapter", "caddyfile"],
        env=runtime_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert validated.returncode == 0, validated.stderr
    for listener in sockets:
        listener.close()

    def request(port, route, host, headers=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", route, headers={"Host": host, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    with (tmp_path / "caddy.log").open("w") as log:
        process = subprocess.Popen(
            [caddy, "run", "--config", str(path), "--adapter", "caddyfile"],
            env=runtime_env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert request(probe_port, "/healthz", "localhost")[0] == 200
                    break
                except OSError:
                    assert process.poll() is None, (tmp_path / "caddy.log").read_text()
                    if time.monotonic() >= deadline:
                        pytest.fail("disposable Caddy listener did not become ready")
                    time.sleep(0.05)

            yield request, consumer_port, admin_port, consumer.server_port, admin.server_port
        finally:
            process.terminate()
            process.wait(timeout=10)
            for server in servers:
                server.shutdown()
                server.server_close()


def test_native_caddy_keeps_admin_and_consumer_boundaries(native_proxy):
    request, consumer_port, admin_port, consumer_backend, admin_backend = native_proxy
    identity = {
        "Cf-Access-Jwt-Assertion": "synthetic.signed.assertion",
        "Cf-Access-Authenticated-User-Email": "spoofed@example.test",
        "X-Goog-IAP-JWT-Assertion": "spoofed-iap",
        "X-Goog-Authenticated-User-Email": "spoofed@example.test",
        "Authorization": "Bearer unrelated",
        "Cookie": "CF_Authorization=unrelated",
        "X-Forwarded-Host": "attacker.test",
        "X-Forwarded-Proto": "http",
    }
    for route in ("/admin", "/admin/v1/session", "/_next/static/operator.js"):
        status, body = request(admin_port, route, "admin-events.iliazlobin.com", identity)
        assert status == 200
        decoded = json.loads(body)
        assert decoded["backend"] == admin_backend
        upstream = {key.lower(): value for key, value in decoded["headers"].items()}
        assert upstream["cf-access-jwt-assertion"] == identity["Cf-Access-Jwt-Assertion"]
        assert upstream["host"] == "admin-events.iliazlobin.com"
        assert upstream["x-forwarded-proto"] == "https"
        assert upstream["x-forwarded-host"] == "admin-events.iliazlobin.com"
        assert not any(key.startswith("x-goog-") for key in upstream)
        assert "cf-access-authenticated-user-email" not in upstream
        assert "authorization" not in upstream and "cookie" not in upstream
    for route in ("/admin", "/admin/v1/session"):
        assert request(admin_port, route, "admin-events.iliazlobin.com")[0] == 404
        assert request(admin_port, route, "attacker.test", identity)[0] == 404
        assert (
            request(
                admin_port,
                route,
                "admin-events.iliazlobin.com",
                {
                    "Cf-Access-Authenticated-User-Email": "iliazlobin91@gmail.com",
                },
            )[0]
            == 404
        )
    for route in ("/v1/events", "/healthz", "/readyz", "/metrics"):
        assert request(admin_port, route, "admin-events.iliazlobin.com", identity)[0] == 404
    for route in ("/admin", "/admin/v1/session", "/healthz", "/readyz", "/metrics"):
        assert request(consumer_port, route, "events.iliazlobin.com", identity)[0] == 404
    assert request(consumer_port, "/v1/events", "admin-events.iliazlobin.com", identity)[0] == 404
    status, body = request(consumer_port, "/v1/events", "events.iliazlobin.com", identity)
    assert status == 200
    decoded = json.loads(body)
    assert decoded["backend"] == consumer_backend
    upstream = {key.lower(): value for key, value in decoded["headers"].items()}
    assert not any(key.startswith(("x-goog-", "cf-access-")) for key in upstream)
    assert request(consumer_port, "/", "events.iliazlobin.com")[0] == 200
