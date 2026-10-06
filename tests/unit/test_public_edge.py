"""Offline GCP edge contracts; optional network-isolated Caddy behavior rehearsal."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/events-concierge"
HOST = "events.iliazlobin.com"


@pytest.fixture(scope="module")
def helm():
    binary = os.environ.get("EC_HELM_BINARY") or shutil.which("helm")
    if not binary:
        pytest.skip("Deployment-validation CI installs Helm")
    return binary


def runtime_values():
    values = yaml.safe_load((CHART / "values-private-authenticated.example.yaml").read_text())
    values["global"].update(
        {
            "releasePhase": "application",
            "runtimeProviderReady": True,
            "releaseRevision": "1" * 40,
            "appImage": {"repository": "validation/app", "digest": "sha256:" + "1" * 64},
            "frontendImage": {"repository": "validation/web", "digest": "sha256:" + "2" * 64},
        }
    )
    return values


def render(binary, directory, values):
    path = directory / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    return subprocess.run(
        [
            binary,
            "template",
            "events-concierge",
            str(CHART),
            "-n",
            "events-concierge-dev",
            "-f",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def documents(result):
    assert result.returncode == 0, result.stderr
    resources = {}
    for item in yaml.safe_load_all(result.stdout):
        if item:
            key = item["kind"], item["metadata"]["name"]
            assert key not in resources
            resources[key] = item
    return resources


def named(resources, kind, suffix):
    return resources[kind, "events-concierge-" + suffix]


def pod(resources, suffix):
    return named(resources, "Deployment", suffix)["spec"]["template"]["spec"]


def labels(resources, suffix):
    return named(resources, "Deployment", suffix)["spec"]["template"]["metadata"]["labels"]


def _selects(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        assert expression["operator"] in {"In", "NotIn"}
        found = labels.get(expression["key"]) in expression["values"]
        if found != (expression["operator"] == "In"):
            return False
    return True


def _allows(
    policies,
    labels,
    direction,
    port,
    *,
    peer_labels=None,
    namespace="events-concierge-dev",
    address=None,
    protocol="TCP",
):
    selected = [
        policy["spec"]
        for policy in policies
        if _selects(policy["spec"]["podSelector"], labels)
        and direction.capitalize() in policy["spec"]["policyTypes"]
    ]
    if not selected:
        return True
    peers_key = "to" if direction == "egress" else "from"
    for policy in selected:
        for rule in policy.get(direction, []):
            if "ports" in rule and not any(
                item["port"] == port and item.get("protocol", "TCP") == protocol
                for item in rule.get("ports", [])
            ):
                continue
            if peers_key not in rule:
                return True
            for peer in rule[peers_key]:
                if "ipBlock" in peer:
                    # GKE Dataplane V2 never admits Pod traffic through ipBlock rules.
                    if peer_labels is None and address:
                        block = peer["ipBlock"]
                        ip = ipaddress.ip_address(address)
                        if ip in ipaddress.ip_network(block["cidr"]) and not any(
                            ip in ipaddress.ip_network(cidr) for cidr in block.get("except", [])
                        ):
                            return True
                elif (
                    peer_labels is not None
                    and ("namespaceSelector" in peer or namespace == "events-concierge-dev")
                    and _selects(peer.get("podSelector", {}), peer_labels)
                    and _selects(
                        peer.get("namespaceSelector", {}),
                        {"kubernetes.io/metadata.name": namespace},
                    )
                ):
                    return True
    return False


def public_values(*, bootstrap=False):
    # Synthetic public identifiers are test fixtures, never deployment inputs.
    values = runtime_values()
    values["operator"] = {
        "enabled": True,
        "hostname": HOST,
        "tlsSecretName": "",
        "iapAudience": "/projects/123456789/global/backendServices/987654321",
        "iapClientId": "validation.apps.googleusercontent.com",
        "iapClientSecretName": "validation-iap-oauth",
        "subjectRoles": {"accounts.google.com:private-validation-owner": "reviewer"},
        "operatorSecrets": [
            {
                "fileName": "EC_OPERATOR_DATABASE_URL",
                "secretName": "ec-dev-operator-database-url",
                "version": "2",
            }
        ],
    }
    values["serviceAccounts"]["operator-api"]["gcpServiceAccount"] = (
        "ec-dev-operator-api@iz27-platform-dev.iam.gserviceaccount.com"
    )
    values["workloads"]["operator-frontend"]["enabled"] = False
    values["workloads"]["operator-api"]["enabled"] = True
    values["publicEdge"] = {
        "enabled": True,
        "bootstrap": bootstrap,
        "operatorAccessVerified": not bootstrap,
        "staticIpName": "ec-public-ip",
        "certificateMap": "ec-public-cert-map",
        "sslPolicy": "ec-public-tls",
    }
    if bootstrap:
        values["operator"].update({"enabled": False, "iapAudience": "", "subjectRoles": {}})
        for name in ("operator-frontend", "operator-api"):
            values["workloads"][name]["enabled"] = False
    return values


@pytest.fixture(scope="module")
def resources(helm, tmp_path_factory):
    result = render(helm, tmp_path_factory.mktemp("public-edge"), public_values())
    rendered = documents(result)
    if output := os.environ.get("EC_DEPLOYMENT_RENDER_DIR"):
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "public-enabled.yaml").write_text(result.stdout)
    return rendered


def test_edge_is_off_without_explicit_configuration(helm, tmp_path):
    rendered = documents(render(helm, tmp_path, runtime_values()))
    assert not any(
        kind in {"Gateway", "HTTPRoute", "GCPBackendPolicy", "GCPGatewayPolicy"}
        for kind, _ in rendered
    )
    assert len(pod(rendered, "frontend")["containers"]) == 1


def test_bootstrap_has_no_admin_endpoints_or_identity_fixtures(helm, tmp_path):
    rendered = documents(render(helm, tmp_path, public_values(bootstrap=True)))
    assert named(rendered, "Service", "operator-frontend")["spec"]["type"] == "ClusterIP"
    selector = named(rendered, "Service", "operator-frontend")["spec"]["selector"]
    for (kind, _), item in rendered.items():
        if kind == "Deployment":
            assert not _selects(
                {"matchLabels": selector}, item["spec"]["template"]["metadata"]["labels"]
            )
    env = {e["name"]: e["value"] for e in pod(rendered, "frontend")["containers"][0]["env"]}
    assert env["EC_OPERATOR_API_ENABLED"] == "false"
    assert "EC_OPERATOR_API_ORIGIN" not in env
    config = named(rendered, "ConfigMap", "public-edge")["data"]["Caddyfile"]
    assert "@admin" not in config and "@assertion" not in config
    policies = [item for (kind, _), item in rendered.items() if kind == "NetworkPolicy"]
    for address in ("130.211.0.1", "35.191.0.1"):
        assert not _allows(policies, labels(rendered, "frontend"), "ingress", 8082, address=address)
    assert named(rendered, "GCPBackendPolicy", "operator-iap")["spec"]["default"]["iap"]["enabled"]
    for kind, name in rendered:
        if "operator" in name:
            assert kind not in {"Deployment", "ConfigMap", "SecretProviderClass"}
    assert not any(kind == "Secret" for kind, _ in rendered)
    assert "private-validation-owner" not in yaml.safe_dump(rendered)
    assert "/projects/123456789" not in yaml.safe_dump(rendered)


def test_one_exact_host_gateway_uses_reserved_ip_certificate_map_and_tls_policy(resources):
    gateways = [item for (kind, _), item in resources.items() if kind == "Gateway"]
    assert len(gateways) == 1
    gateway = gateways[0]
    assert gateway["metadata"]["annotations"] == {"networking.gke.io/certmap": "ec-public-cert-map"}
    assert gateway["spec"]["gatewayClassName"] == "gke-l7-global-external-managed"
    assert gateway["spec"]["addresses"] == [{"type": "NamedAddress", "value": "ec-public-ip"}]
    listeners = gateway["spec"]["listeners"]
    assert {(x["hostname"], x["port"], x["protocol"]) for x in listeners} == {
        (host, port, protocol)
        for host in (HOST,)
        for port, protocol in ((80, "HTTP"), (443, "HTTPS"))
    }
    for listener in listeners:
        assert listener["allowedRoutes"] == {"namespaces": {"from": "Same"}}
        if listener["protocol"] == "HTTPS":
            assert "tls" not in listener
    policy = named(resources, "GCPGatewayPolicy", "public-tls")["spec"]
    assert policy["default"] == {"sslPolicy": "ec-public-tls"}
    assert policy["targetRef"] == {
        "group": "gateway.networking.k8s.io",
        "kind": "Gateway",
        "name": "events-concierge-public",
    }


def test_routes_separate_consumer_admin_and_https_redirects(resources):
    routes = [item for (kind, _), item in resources.items() if kind == "HTTPRoute"]
    assert len(routes) == 3
    for role in ("public",):
        redirect = named(resources, "HTTPRoute", "https-redirect")["spec"]
        assert redirect["parentRefs"] == [
            {"name": "events-concierge-public", "sectionName": role + "-http"}
        ]
        # Omitting host/path/query/port overrides preserves the original URL on HTTPS.
        assert redirect["rules"] == [
            {
                "filters": [
                    {
                        "type": "RequestRedirect",
                        "requestRedirect": {
                            "scheme": "https",
                            "statusCode": 301,
                        },
                    }
                ]
            }
        ]
    consumer = named(resources, "HTTPRoute", "consumer")["spec"]
    assert consumer["hostnames"] == [HOST]
    assert consumer["rules"] == [
        {"backendRefs": [{"name": "events-concierge-frontend", "port": 80}]}
    ]
    admin = named(resources, "HTTPRoute", "operator")["spec"]
    assert admin["hostnames"] == [HOST]
    assert {x["path"]["value"] for x in admin["rules"][0]["matches"]} == {
        "/admin",
    }
    policies = [item for (kind, _), item in resources.items() if kind == "GCPBackendPolicy"]
    assert len(policies) == 1
    assert policies[0]["spec"]["targetRef"]["name"] == "events-concierge-operator-frontend"
    assert policies[0]["spec"]["default"]["iap"] == {
        "enabled": True,
        "clientID": "validation.apps.googleusercontent.com",
        "oauth2ClientSecret": {"name": "validation-iap-oauth"},
    }
    assert admin["parentRefs"][0]["sectionName"] == "public-https"
    # IAP returns to the original /admin URL with its query; public / stays public.
    assert {"path": {"type": "Exact", "value": "/admin"}} in admin["rules"][0]["matches"]
    assert {"path": {"type": "PathPrefix", "value": "/admin"}} in admin["rules"][0]["matches"]
    assert admin["rules"][0]["backendRefs"] == [
        {"name": policies[0]["spec"]["targetRef"]["name"], "port": 80}
    ]


def test_public_filter_is_a_credential_free_sidecar_in_existing_frontend(resources):
    spec = pod(resources, "frontend")
    app, edge = spec["containers"]
    env = {e["name"]: e["value"] for e in app["env"]}
    assert env["EC_API_ORIGIN"] == "http://events-concierge-api:8000"
    assert env["EC_OPERATOR_API_ENABLED"] == "true"
    assert env["EC_OPERATOR_API_ORIGIN"] == "http://events-concierge-operator-api:8000"
    assert env["EC_OPERATOR_PUBLIC_ORIGIN"] == "https://" + HOST
    assert app["name"] == "frontend" and edge["name"] == "public-edge"
    assert (
        edge["image"]
        == "caddy@sha256:d44355d3c2149dc580ce2cac735955d1c08d3d00882c30489c241aa51a5c10d9"
    )
    assert not spec["automountServiceAccountToken"]
    assert not edge.get("envFrom")
    assert {m["name"] for m in edge["volumeMounts"]} == {"public-edge-config", "edge-tmp"}
    assert edge["securityContext"]["readOnlyRootFilesystem"]
    assert not edge["securityContext"]["allowPrivilegeEscalation"]
    assert edge["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert edge["command"] == ["/bin/sh", "-ec"]
    assert edge["args"] == [
        "cp /usr/bin/caddy /tmp/caddy; exec /tmp/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"
    ]
    assert edge["resources"] == {
        "requests": {"cpu": "10m", "memory": "32Mi"},
        "limits": {"cpu": "100m", "memory": "128Mi"},
    }
    assert named(resources, "Service", "frontend")["spec"]["ports"][0]["targetPort"] == 8080
    admin = named(resources, "Service", "operator-frontend")["spec"]
    assert admin["ports"][0]["targetPort"] == 8082
    assert admin["selector"] == named(resources, "Service", "frontend")["spec"]["selector"]
    assert ("ServiceAccount", "events-concierge-operator-frontend") not in resources
    assert {p["containerPort"] for p in edge["ports"]} == {8080, 8081, 8082}
    admin_health = named(resources, "HealthCheckPolicy", "operator-frontend")["spec"]["default"][
        "config"
    ]["httpHealthCheck"]
    assert admin_health == {
        "portSpecification": "USE_FIXED_PORT",
        "port": 8081,
        "requestPath": "/readyz",
    }
    health = named(resources, "HealthCheckPolicy", "frontend")["spec"]["default"]["config"][
        "httpHealthCheck"
    ]
    assert health == {"portSpecification": "USE_FIXED_PORT", "port": 8081, "requestPath": "/readyz"}
    assert edge["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "edge-health"}
    assert {name for kind, name in resources if kind == "Deployment"} == {
        "events-concierge-" + n
        for n in (
            "api",
            "frontend",
            "account-erasure",
            "temporal-catalog",
            "ingestion-executor",
            "operator-api",
        )
    }


@pytest.mark.parametrize(
    "change",
    [
        {"global": {"deploymentProfile": "development"}},
        {"global": {"releasePhase": "migration"}},
        {"global": {"runtimeProviderReady": False}},
        {"applicationConfig": {"EC_MOCK_CLOUD": "true"}},
        {"applicationConfig": {"EC_PUBLIC_BASE_URL": "https://unapproved.example"}},
        {"applicationConfig": {"EC_IDENTITY_PLATFORM_ENABLED": "false"}},
        {"applicationConfig": {"EC_ADMIN_INGESTION_ENABLED": "true"}},
        {"applicationConfig": {"EC_OIDC_BFF_ENABLED": "true"}},
        {"networkPolicy": {"enabled": False}},
        {"gateway": {"enabled": True}},
        {"publicEdge": {"staticIpName": ""}},
        {"publicEdge": {"certificateMap": ""}},
        {"publicEdge": {"sslPolicy": ""}},
        {"publicEdge": {"hostname": "*.iliazlobin.com"}},
        {"publicEdge": {"proxyImage": {"repository": "unknown"}}},
        {"operator": {"enabled": False}},
        {"operator": {"iapAudience": ""}},
        {"operator": {"subjectRoles": {}}},
        {"operator": {"tlsSecretName": "old-tls"}},
        {"operator": {"iapClientSecretName": "ec-consumer-google"}},
        {"operator": {"authProvider": "cloudflare_access"}},
        {"publicEdge": {"bootstrap": True}},
        {"publicEdge": {"operatorAccessVerified": False}},
        {"operator": {"hostname": "admin-events.iliazlobin.com"}},
        {"workloads": {"operator-frontend": {"enabled": True}}},
        {"workloads": {"frontend": {"command": ["node", "alternate.js"]}}},
        {"publicTunnel": {"enabled": False}},
    ],
)
def test_incomplete_or_conflicting_edge_configuration_fails_before_apply(helm, tmp_path, change):
    config = public_values()
    for key, fields in change.items():
        config.setdefault(key, {}).update(fields)
    assert render(helm, tmp_path, config).returncode != 0


def test_combined_policies_limit_gfe_ingress_and_preserve_store_isolation(
    helm, tmp_path, resources
):
    data = subprocess.run(
        [
            helm,
            "template",
            "ec-dev-stores",
            str(ROOT / "deploy/helm/events-concierge-dev-data"),
            "-n",
            "events-concierge-dev",
            "-f",
            str(ROOT / "deploy/helm/events-concierge-dev-data/values-shared-development.yaml"),
            "-f",
            str(ROOT / "deploy/helm/events-concierge-dev-data/values-private-tls.yaml"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    combined = resources | documents(data)
    policies = [item for (kind, _), item in combined.items() if kind == "NetworkPolicy"]
    consumer, api = (labels(combined, x) for x in ("frontend", "operator-api"))
    for address in ("130.211.0.1", "35.191.0.1"):
        for port in (8080, 8081, 8082):
            assert _allows(policies, consumer, "ingress", port, address=address)
        assert not _allows(policies, consumer, "ingress", 3000, address=address)
        assert not _allows(policies, api, "ingress", 8000, address=address)
    for peer in (
        api,
        {"app.kubernetes.io/part-of": "events-concierge", "app.kubernetes.io/component": "unknown"},
    ):
        for port in (3000, 8080, 8081, 8082):
            assert not _allows(policies, consumer, "ingress", port, peer_labels=peer)
    assert not _allows(policies, consumer, "ingress", 8080, address="203.0.113.10")
    assert _allows(policies, api, "ingress", 8000, peer_labels=consumer)
    assert _allows(policies, consumer, "egress", 8000, peer_labels=api)
    consumer_api = labels(combined, "api")
    assert _allows(policies, consumer, "egress", 8000, peer_labels=consumer_api)
    assert _allows(policies, consumer_api, "ingress", 8000, peer_labels=consumer)
    assert not _allows(policies, consumer, "egress", 80, address="169.254.169.254")
    assert not _allows(policies, consumer, "egress", 443, address="203.0.113.10")
    for dns in ("kube-dns", "node-local-dns"):
        for protocol in ("TCP", "UDP"):
            assert _allows(
                policies,
                consumer,
                "egress",
                53,
                peer_labels={"k8s-app": dns},
                namespace="kube-system",
                protocol=protocol,
            )
    for role in (consumer, api):
        for kind, name, port in (
            ("Deployment", "ec-dev-redis", 6379),
            ("StatefulSet", "ec-dev-temporal-postgres", 5432),
        ):
            store = combined[kind, name]["spec"]["template"]["metadata"]["labels"]
            assert not _allows(policies, store, "ingress", port, peer_labels=role)
            assert not _allows(policies, role, "egress", port, peer_labels=store)
    temporal_frontend = {
        "app.kubernetes.io/part-of": "events-concierge",
        "app.kubernetes.io/name": "temporal",
        "app.kubernetes.io/component": "frontend",
    }
    catalog = labels(combined, "temporal-catalog")
    assert _allows(policies, temporal_frontend, "ingress", 7233, peer_labels=catalog)
    assert _allows(policies, catalog, "egress", 7233, peer_labels=temporal_frontend)


@pytest.fixture(scope="module")
def proxy_rehearsal(resources, tmp_path_factory):
    # Native mode is an offline loopback rehearsal; CI uses the pinned container below.
    if binary := os.environ.get("EC_CADDY_BINARY"):
        with _native_rehearsal(
            resources, tmp_path_factory.mktemp("edge-native"), binary
        ) as request:
            yield request
        return
    if os.environ.get("EC_PUBLIC_EDGE_DOCKER") != "1":
        pytest.skip("Set EC_PUBLIC_EDGE_DOCKER=1 for disposable local/CI edge tests")
    docker = shutil.which("docker")
    assert docker, "The requested edge rehearsal requires Docker"
    pod = named(resources, "Deployment", "frontend")["spec"]["template"]["spec"]
    image = pod["containers"][1]["image"]
    config = named(resources, "ConfigMap", "public-edge")["data"]["Caddyfile"]
    config = config.replace("127.0.0.1:3000", "127.0.0.1:8083")
    config += _echo_upstream(8083)
    name = f"ec-public-edge-test-{uuid.uuid4().hex[:12]}"

    def command(*args, data=None, timeout=60):
        return subprocess.run(
            [docker, *args],
            input=data,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    script = (
        "while [ ! -f /tmp/Caddyfile ]; do sleep 1; done; "
        "cp /usr/bin/caddy /tmp/caddy; "
        "exec /tmp/caddy run --config /tmp/Caddyfile --adapter caddyfile"
    )
    start = command(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "128m",
        "--cpus",
        "0.1",
        "--tmpfs",
        "/tmp:uid=10001,gid=10001,size=134217728,exec",
        "-e",
        "XDG_CONFIG_HOME=/tmp/config",
        "-e",
        "XDG_DATA_HOME=/tmp/data",
        "--entrypoint",
        "sh",
        image,
        "-c",
        script,
    )
    try:
        assert start.returncode == 0, start.stderr
        injected = command(
            "exec",
            "-i",
            name,
            "sh",
            "-c",
            "cat > /tmp/Caddyfile.pending && mv /tmp/Caddyfile.pending /tmp/Caddyfile",
            data=config,
        )
        assert injected.returncode == 0, injected.stderr
        try:
            ready = command(
                "exec",
                name,
                "sh",
                "-c",
                "for n in $(seq 1 30); do wget -T 2 -q -O /dev/null http://127.0.0.1:8081/readyz && exit 0; sleep 2; done; exit 1",
                timeout=120,
            )
            startup_error = (
                f"Readiness exited {ready.returncode}: {ready.stderr}" if ready.returncode else ""
            )
        except subprocess.TimeoutExpired as exc:
            startup_error = str(exc)
        if startup_error:
            logs = command("logs", name)
            state = command(
                "inspect",
                "--format",
                "Running={{.State.Running}} OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}}",
                name,
            )
            pytest.fail(startup_error + logs.stderr + logs.stdout + state.stdout)

        def request(path, host=HOST, headers=(), method="GET", port=8080):
            assert port in (8080, 8082)
            extra_headers = "".join(f"{key}: {value}\r\n" for key, value in headers)
            # Keep stdin open while both proxy hops respond; BusyBox nc otherwise
            # cancels the request as soon as docker exec delivers input EOF.
            result = command(
                "exec",
                "-i",
                name,
                "sh",
                "-c",
                f"(cat; sleep 1) | nc -w 5 127.0.0.1 {port}",
                data=f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n{extra_headers}Connection: close\r\n\r\n",
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        yield request
    finally:
        cleanup = command("rm", "-f", name)
        assert cleanup.returncode == 0, cleanup.stderr


def _echo_upstream(port):
    # Presence/format test only. Real signature/audience verification belongs to the API.
    echo = "host={http.request.host} proto={http.request.header.X-Forwarded-Proto} forwarded={http.request.header.Forwarded} identity={http.request.header.X-Goog-Authenticated-User-Email} access={http.request.header.Cf-Access-Authenticated-User-Email} middleware={http.request.header.X-Middleware-Subrequest} jwt={http.request.header.X-Goog-IAP-JWT-Assertion} uri={http.request.uri}"
    return f"""\nhttp://:{port} {{
  bind 127.0.0.1
  @private path /admin /admin/*
  respond @private "PRIVATE_FIXTURE {echo}" 200
  respond "PUBLIC_FIXTURE {echo}" 200
}}\n"""


@contextmanager
def _native_rehearsal(resources, directory, binary):
    ports = set()
    while len(ports) < 4:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.add(sock.getsockname()[1])
    edge, admin, health, upstream = ports
    config = named(resources, "ConfigMap", "public-edge")["data"]["Caddyfile"]
    for old, new in ((8080, edge), (8082, admin), (8081, health)):
        config = config.replace(f"http://:{old} {{", f"http://:{new} {{\n  bind 127.0.0.1")
    config = config.replace("127.0.0.1:3000", f"127.0.0.1:{upstream}") + _echo_upstream(upstream)
    path = directory / "Caddyfile"
    path.write_text(config)
    validate = subprocess.run(
        [binary, "validate", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert validate.returncode == 0, validate.stderr
    with (directory / "caddy.log").open("w") as log:
        process = subprocess.Popen(
            [binary, "run", "--config", str(path), "--adapter", "caddyfile"], stdout=log, stderr=log
        )
        try:
            for _ in range(50):
                assert process.poll() is None, (directory / "caddy.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", health), timeout=1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError("Loopback Caddy startup timed out")

            def request(path, host=HOST, headers=(), method="GET", port=8080):
                assert port in (8080, 8082)
                extra = "".join(f"{key}: {value}\r\n" for key, value in headers)
                data = (
                    f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n{extra}Connection: close\r\n\r\n"
                )
                with socket.create_connection(
                    ("127.0.0.1", edge if port == 8080 else admin), timeout=5
                ) as sock:
                    sock.sendall(data.encode())
                    chunks = []
                    while chunk := sock.recv(65536):
                        chunks.append(chunk)
                    return b"".join(chunks).decode()

            yield request
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("path", ["/", "/sign-in", "/v1/events?city=sanfrancisco"])
def test_consumer_pages_and_api_remain_available(proxy_rehearsal, path):
    response = proxy_rehearsal(path)
    assert "200 OK" in response.splitlines()[0]
    assert "PUBLIC_FIXTURE" in response


def test_forwarded_authority_is_rebuilt_and_operator_headers_are_removed(proxy_rehearsal):
    spoofed = "UNTRUSTED_FIXTURE"
    response = proxy_rehearsal(
        "/",
        headers=[
            ("Forwarded", spoofed),
            ("X-Forwarded-Host", spoofed),
            ("X-Forwarded-Proto", "http"),
            ("X-Goog-Authenticated-User-Email", spoofed),
            ("Cf-Access-Authenticated-User-Email", spoofed),
            ("X-Middleware-Subrequest", spoofed),
        ],
    )
    assert "200 OK" in response.splitlines()[0]
    assert "host=events.iliazlobin.com proto=https" in response
    assert spoofed not in response


@pytest.mark.parametrize(
    "path",
    [
        "/admin/review",
        "/admin/v1/ingestion",
        "/admin/v1/ingestion?next=/admin",
        "/administrator",
        "/admin//",
        "/admin/.",
        "/admin/%2e",
        "/%61dmin/",
        "/admin%2f",
        "//admin/",
        "/v1/../admin/",
        "/v1/%2e%2e/admin/",
        "/%2fadmin/",
        "/healthz",
        "/readyz",
        "/metrics",
        "/_next/image?url=/admin&w=640&q=75",
        "/_next/data/build/admin.json",
        "/v1/%252e%252e/admin",
        "/v1/a%5cb",
    ],
)
def test_private_and_probe_paths_never_reach_the_frontend(proxy_rehearsal, path):
    response = proxy_rehearsal(path)
    assert "404 Not Found" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" not in response and "PUBLIC_FIXTURE" not in response


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize(
    "path",
    ["/admin", "/admin/", "/admin?gcp-iap-mode=AUTHENTICATING", "/admin/v1/operator/session"],
)
def test_public_port_never_proxies_admin_even_with_an_assertion(proxy_rehearsal, path, method):
    response = proxy_rehearsal(
        path, method=method, headers=[("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes")]
    )
    assert "404 Not Found" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" not in response and "PUBLIC_FIXTURE" not in response


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/",
        "/admin?gcp-iap-mode=AUTHENTICATING",
        "/admin/v1/operator/session?cursor=one%2Ftwo",
    ],
)
def test_protected_port_preserves_admin_query_and_only_assertion(proxy_rehearsal, path):
    response = proxy_rehearsal(
        path,
        port=8082,
        headers=[
            ("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes"),
            ("X-Goog-Authenticated-User-Email", "UNTRUSTED_FIXTURE"),
            ("Cf-Access-Jwt-Assertion", "UNTRUSTED_FIXTURE"),
            ("Forwarded", "UNTRUSTED_FIXTURE"),
            ("X-Real-IP", "UNTRUSTED_FIXTURE"),
            ("X-Forwarded-Host", "UNTRUSTED_FIXTURE"),
            ("X-Middleware-Subrequest", "UNTRUSTED_FIXTURE"),
        ],
    )
    assert "200 OK" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" in response and "jwt=signed.jwt.bytes" in response
    assert "host=events.iliazlobin.com proto=https" in response
    assert "uri=" + path in response and "UNTRUSTED_FIXTURE" not in response


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Goog-IAP-JWT-Assertion", "")],
        [("X-Goog-IAP-JWT-Assertion", "not-a-jwt")],
        [("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes,other.jwt.bytes")],
        [
            ("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes"),
            ("X-Goog-IAP-JWT-Assertion", "other.jwt.bytes"),
        ],
        [("X-Goog-IAP-JWT-Assertion", "a" * 8193 + ".b.c")],
    ],
)
def test_protected_port_rejects_missing_ambiguous_or_overlong_assertion(proxy_rehearsal, headers):
    response = proxy_rehearsal("/admin", port=8082, headers=headers)
    assert "401 Unauthorized" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" not in response


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/v1/events",
        "/_next/static/app.js",
        "/admin//",
        "/admin/.",
        "/admin/..",
        "/admin/%2e",
        "/admin/%2E%2e/v1",
        "/%61dmin",
        "/admin%2f",
        "/admin%5c",
        "/admin/%252e",
        "/admin/../v1",
        "//admin",
        "/ADMIN",
        "/administrator",
    ],
)
def test_protected_port_rejects_non_admin_and_ambiguous_raw_paths(proxy_rehearsal, path):
    response = proxy_rehearsal(
        path, port=8082, headers=[("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes")]
    )
    assert "404 Not Found" in response.splitlines()[0]
    assert "PRIVATE_FIXTURE" not in response and "PUBLIC_FIXTURE" not in response


@pytest.mark.parametrize(
    "path",
    [
        "/_next/static/chunks/%5Bslug%5D.js",
        "/v1/catalog/entities/person%3Aabc",
        "/v1/catalog/entities/Jos%C3%A9",
        "/?next=%2Fadmin",
    ],
)
def test_safe_encoded_segments_and_query_data_remain_public(proxy_rehearsal, path):
    response = proxy_rehearsal(path)
    assert "200 OK" in response.splitlines()[0]
    assert "PUBLIC_FIXTURE" in response


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "hermes.iliazlobin.com",
        "other.example.com",
        "events.iliazlobin.com:3000",
    ],
)
@pytest.mark.parametrize("path", ["/", "/admin", "/admin/"])
@pytest.mark.parametrize("port", [8080, 8082])
def test_unapproved_host_cannot_become_a_loopback_admin_request(proxy_rehearsal, host, path, port):
    response = proxy_rehearsal(
        path, host, port=port, headers=[("X-Goog-IAP-JWT-Assertion", "signed.jwt.bytes")]
    )
    assert "404 Not Found" in response.splitlines()[0]
    assert "Location:" not in response
    assert "PUBLIC_FIXTURE" not in response
