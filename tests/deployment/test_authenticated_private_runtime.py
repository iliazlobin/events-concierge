"""Full offline render and real startup contracts for authenticated in-cluster discovery."""

from __future__ import annotations

import copy
import ipaddress
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from events_concierge.config import Settings
from events_concierge.deployment.startup import (
    preflight_catalog_runtime,
    preflight_operator_runtime,
)
from events_concierge.operations.config_validation import validate_production_config

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/events-concierge"
BACKENDS = ("api", "account-erasure", "ingestion-executor", "temporal-catalog")
TLS_ROOT = "/var/run/events-concierge-tls"


@pytest.fixture(scope="module")
def helm() -> str:
    binary = os.environ.get("EC_HELM_BINARY") or os.environ.get("HELM") or shutil.which("helm")
    if not binary:
        pytest.skip("Helm is optional locally; CI runs the complete deployment contracts")
    return binary


def values() -> dict[str, Any]:
    result = yaml.safe_load((CHART / "values-private-authenticated.example.yaml").read_text())
    result["global"].update(
        {
            "releasePhase": "application",
            "runtimeProviderReady": True,
            "releaseRevision": "1" * 40,
            "appImage": {"repository": "validation/app", "digest": "sha256:" + "1" * 64},
            "frontendImage": {"repository": "validation/web", "digest": "sha256:" + "2" * 64},
        }
    )
    result["privateRuntime"]["cadenceEnabled"] = True
    return result


def render(
    helm: str, directory: Path, override: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    path = directory / "values.yaml"
    path.write_text(yaml.safe_dump(override))
    return subprocess.run(
        [
            helm,
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


@pytest.fixture(scope="module")
def resources(helm: str, tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    result = render(helm, tmp_path_factory.mktemp("authenticated-render"), values())
    assert result.returncode == 0, result.stderr
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def pod(resources: dict[tuple[str, str], Any], name: str) -> dict[str, Any]:
    return resources["Deployment", "events-concierge-" + name]["spec"]["template"]["spec"]


def test_only_discovery_processes_and_cadence_are_exposed(resources):
    assert {name for kind, name in resources if kind == "Deployment"} == {
        "events-concierge-" + name for name in ("frontend", *BACKENDS)
    }
    assert {name for kind, name in resources if kind == "CronJob"} == {
        "events-concierge-ingestion-cadence"
    }
    assert not any(
        kind in {"Secret", "Ingress", "Gateway", "HTTPRoute", "HorizontalPodAutoscaler"}
        for kind, _ in resources
    )
    assert all(
        doc["spec"].get("type", "ClusterIP") == "ClusterIP"
        for (kind, _), doc in resources.items()
        if kind == "Service"
    )
    assert pod(resources, "frontend")["automountServiceAccountToken"] is False
    assert {v["name"] for v in pod(resources, "frontend")["volumes"]} == {"tmp"}


def test_processes_get_only_their_own_credentials_and_client_key(resources):
    client_names = set()
    for name in BACKENDS:
        spec = pod(resources, name)
        assert "initContainers" not in spec
        container = spec["containers"][0]
        assert container["envFrom"] == [
            {"configMapRef": {"name": "events-concierge-private-" + name}}
        ]
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert all(
            mounts[key]["readOnly"]
            for key in (
                "postgres-ca",
                "redis-ca",
                "temporal-ca",
                "temporal-client",
                "runtime-secrets",
            )
        )
        volumes = {v["name"]: v for v in spec["volumes"]}
        client = volumes["temporal-client"]["secret"]
        client_names.add(client["secretName"])
        assert client["defaultMode"] == 0o440
        assert {item["key"] for item in client["items"]} == {"tls.crt", "tls.key"}
        for key in ("postgres-ca", "redis-ca", "temporal-ca"):
            assert volumes[key]["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
        spc = resources["SecretProviderClass", "events-concierge-private-" + name]
        files = yaml.safe_load(spc["spec"]["parameters"]["secrets"])
        database = (
            "EC_INGESTION_EXECUTOR_DATABASE_URL" if name in BACKENDS[2:] else "EC_DATABASE_URL"
        )
        assert {f["path"] for f in files} == {database, "EC_REDIS_URL"}
        assert all("/versions/2" in f["resourceName"] for f in files)
    assert len(client_names) == len(BACKENDS)
    assert ("SecretProviderClass", "events-concierge-runtime") not in resources


def test_cadence_has_no_consumer_identity_redis_or_temporal_credentials(resources):
    cron = resources["CronJob", "events-concierge-ingestion-cadence"]
    spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert {v["name"] for v in spec["volumes"]} == {"tmp", "runtime-secrets", "postgres-ca"}
    config = resources["ConfigMap", "events-concierge-private-controller"]["data"]
    assert not any(
        key.startswith("EC_IDENTITY_PLATFORM_") and key != "EC_IDENTITY_PLATFORM_ENABLED"
        for key in config
    )
    assert config["EC_IDENTITY_PLATFORM_ENABLED"] == "false"
    assert config["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"] == "true"
    assert not any("TLS_CLIENT" in key or "REDIS" in key for key in config)
    files = yaml.safe_load(
        resources["SecretProviderClass", "events-concierge-private-controller"]["spec"][
            "parameters"
        ]["secrets"]
    )
    assert len(files) == 1 and files[0]["path"] == "EC_OPERATOR_DATABASE_URL"


def test_explicit_legal_deferral_is_scoped_to_consumer_identity_processes(helm, tmp_path):
    configured = values()
    configured["applicationConfig"]["EC_CONSUMER_LEGAL_MODE"] = "deferred"
    legal_fields = (
        "EC_SIGNUP_TERMS_VERSION",
        "EC_SIGNUP_TERMS_URL",
        "EC_SIGNUP_PRIVACY_VERSION",
        "EC_SIGNUP_PRIVACY_URL",
    )
    configured["applicationConfig"].update(dict.fromkeys(legal_fields, ""))
    result = render(helm, tmp_path, configured)
    assert result.returncode == 0, result.stderr
    configs = {
        d["metadata"]["name"]: d["data"]
        for d in yaml.safe_load_all(result.stdout)
        if d and d["kind"] == "ConfigMap" and "-private-" in d["metadata"]["name"]
    }
    for process, config in configs.items():
        if process in {"events-concierge-private-api", "events-concierge-private-account-erasure"}:
            assert config["EC_CONSUMER_LEGAL_MODE"] == "deferred"
            assert all(config[field] == "" for field in legal_fields)
        else:
            assert "EC_CONSUMER_LEGAL_MODE" not in config
            assert not any(field in config for field in legal_fields)


def _mtls_files(directory: Path) -> dict[str, str]:
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "disposable test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "disposable test client")])
        )
        .issuer_name(issuer)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
        .sign(key, hashes.SHA256())
    )
    material = {
        f"{TLS_ROOT}/temporal/ca.crt": ca.public_bytes(serialization.Encoding.PEM),
        f"{TLS_ROOT}/client/tls.crt": cert.public_bytes(serialization.Encoding.PEM),
        f"{TLS_ROOT}/client/tls.key": client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    }
    paths = {}
    for index, (name, data) in enumerate(material.items()):
        path = directory / f"material-{index}.pem"
        path.write_bytes(data)
        path.chmod(0o600)
        paths[name] = str(path)
    return paths


@pytest.mark.parametrize("name", (*BACKENDS, "controller"))
def test_rendered_process_settings_satisfy_real_startup_contracts(resources, tmp_path, name):
    config = copy.deepcopy(resources["ConfigMap", "events-concierge-private-" + name]["data"])
    files = _mtls_files(tmp_path)
    for key, value in list(config.items()):
        if value in files:
            config[key] = files[value]
        elif key.endswith("_URL_FILE"):
            del config[key]
    role = "ec_app" if name in BACKENDS[:2] else "ec_dev_ingestion"
    config["EC_DATABASE_URL"] = (
        f"postgresql+psycopg://{role}:fixture@ec-dev-application-postgres.events-concierge-dev.svc.cluster.local/events?sslmode=verify-full"
    )
    config["EC_REDIS_URL"] = (
        "rediss://:fixture@ec-dev-redis.events-concierge-dev.svc.cluster.local/0?ssl_cert_reqs=required&ssl_check_hostname=true"
    )
    if name in BACKENDS[2:]:
        config["EC_INGESTION_EXECUTOR_DATABASE_URL"] = config["EC_DATABASE_URL"]
    if name == "controller":
        config["EC_OPERATOR_DATABASE_URL"] = (
            "postgresql+psycopg://ec_dev_operator:fixture@ec-dev-application-postgres.events-concierge-dev.svc.cluster.local/events?sslmode=verify-full"
        )
    with patch.dict(os.environ, config, clear=True):
        settings = Settings(_env_file=None)
    if name in BACKENDS[:2]:
        report = validate_production_config(settings)
        assert report.passed, [c.name for c in report.checks if not c.passed]
    elif name in BACKENDS[2:]:
        preflight_catalog_runtime(settings)
    else:
        preflight_operator_runtime(settings)


@pytest.mark.parametrize(
    "mutation",
    [
        "mock",
        "plaintext",
        "no_identity",
        "no_ca",
        "shared_client",
        "shared_identity",
        "unscoped_secret",
        "owner_secret",
        "server_key",
        "floating_secret",
        "workload_override",
        "demo",
        "deferred",
        "controller_secret",
        "runtime_bundle",
        "no_isolation",
    ],
)
def test_unsafe_compositions_fail_before_any_workload_is_created(helm, tmp_path, mutation):  # noqa: PLR0912 - explicit security mutations
    config = values()
    if mutation == "mock":
        config["applicationConfig"]["EC_MOCK_CLOUD"] = "true"
    elif mutation == "plaintext":
        config["applicationConfig"]["EC_DATABASE_CONNECTION_MODE"] = "development_plaintext"
    elif mutation == "no_identity":
        config["applicationConfig"]["EC_IDENTITY_PLATFORM_ENABLED"] = "false"
    elif mutation == "no_ca":
        config["privateRuntime"]["postgresCASecretName"] = ""
    elif mutation == "shared_client":
        config["privateRuntime"]["temporalClientSecretNames"]["api"] = config["privateRuntime"][
            "temporalClientSecretNames"
        ]["account-erasure"]
    elif mutation == "shared_identity":
        config["serviceAccounts"]["api"] = config["serviceAccounts"]["account-erasure"]
    elif mutation == "owner_secret":
        config["privateRuntime"]["processSecrets"]["api"][0]["secretName"] = "ec-dev-migration-url"
    elif mutation == "server_key":
        config["privateRuntime"]["temporalClientSecretNames"]["api"] = "ec-dev-temporal-tls-v1"
    elif mutation in {"unscoped_secret", "floating_secret"}:
        config["privateRuntime"]["processSecrets"]["api"][0][
            "fileName" if mutation == "unscoped_secret" else "version"
        ] = "EC_MIGRATION_URL" if mutation == "unscoped_secret" else "latest"
    elif mutation == "workload_override":
        config["workloads"]["api"]["env"] = {"EC_MOCK_CLOUD": "true"}
    elif mutation == "demo":
        config["developmentCatalog"] = {"enabled": True}
    elif mutation == "deferred":
        config["workloads"]["notifier"] = {"enabled": True}
    elif mutation == "controller_secret":
        config["privateRuntime"]["controllerSecrets"][0]["fileName"] = "EC_DATABASE_URL"
    elif mutation == "runtime_bundle":
        config["secretManager"]["runtimeSecrets"] = [
            {"fileName": "EC_DATABASE_URL", "secretName": "unscoped", "version": "1"}
        ]
    else:
        config["networkPolicy"] = {"enabled": False}
    result = render(helm, tmp_path, config)
    assert result.returncode != 0, f"unsafe {mutation} profile rendered"


def test_migration_mounts_only_its_ca_and_owner_credential(helm, tmp_path):
    config = values()
    config["global"]["releasePhase"] = "migration"
    result = render(helm, tmp_path, config)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert not any(doc["kind"] in {"Deployment", "CronJob"} for doc in docs)
    job = next(doc for doc in docs if doc["kind"] == "Job")
    spec = job["spec"]["template"]["spec"]
    assert {v["name"] for v in spec["volumes"]} == {"tmp", "migration-secrets", "postgres-ca"}
    assert spec["containers"][0]["command"] == ["alembic", "upgrade", "head"]
    env = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
    assert env["EC_DATABASE_CONNECTION_MODE"] == "direct_tls"
    assert env["EC_MOCK_CLOUD"] == "false"


def test_certificate_rotation_uses_a_new_immutable_migration_job(helm, tmp_path):
    config = values()
    config["global"]["releasePhase"] = "migration"
    first = render(helm, tmp_path, config)
    config["privateRuntime"]["postgresCASecretName"] = "ec-dev-postgres-ca-v2"
    second = render(helm, tmp_path, config)
    assert first.returncode == second.returncode == 0
    names = [
        next(
            doc["metadata"]["name"]
            for doc in yaml.safe_load_all(result.stdout)
            if doc and doc["kind"] == "Job"
        )
        for result in (first, second)
    ]
    assert names[0] != names[1]


def test_cadence_stays_absent_until_explicit_acceptance(helm, tmp_path):
    config = values()
    config["privateRuntime"]["cadenceEnabled"] = False
    result = render(helm, tmp_path, config)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert not any(doc["kind"] == "CronJob" for doc in docs)
    assert not any(doc["metadata"]["name"].endswith("private-controller") for doc in docs)


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


def test_combined_chart_policies_isolate_public_connector_from_backend_authority(helm, tmp_path):
    config = values()
    config["publicTunnel"] = {"enabled": True, "tokenSecretName": "ec-cloudflare-tunnel-v1"}
    application = render(helm, tmp_path, config)
    assert application.returncode == 0, application.stderr
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
    docs = [
        doc for result in (application, data) for doc in yaml.safe_load_all(result.stdout) if doc
    ]
    policies = [doc for doc in docs if doc["kind"] == "NetworkPolicy"]

    def labels(name):
        return next(
            doc
            for doc in docs
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "events-concierge-" + name
        )["spec"]["template"]["metadata"]["labels"]

    connector, frontend, api = labels("public-tunnel"), labels("frontend"), labels("api")
    assert _allows(policies, connector, "egress", 3000, peer_labels=frontend)
    assert _allows(policies, frontend, "ingress", 3000, peer_labels=connector)
    assert _allows(policies, frontend, "egress", 8000, peer_labels=api)
    assert _allows(policies, api, "ingress", 8000, peer_labels=frontend)
    for port in (5432, 6379, 7233, 8000):
        assert not _allows(policies, connector, "egress", port, peer_labels=api)
        assert not _allows(policies, api, "ingress", port, peer_labels=connector)
    assert not _allows(policies, connector, "egress", 80, address="169.254.169.254")
    assert not _allows(policies, connector, "egress", 443, address="203.0.113.10")
    for protocol in ("TCP", "UDP"):
        assert _allows(
            policies, connector, "egress", 7844, address="198.41.192.1", protocol=protocol
        )
        for dns in ("kube-dns", "node-local-dns"):
            assert _allows(
                policies,
                connector,
                "egress",
                53,
                peer_labels={"k8s-app": dns},
                namespace="kube-system",
                protocol=protocol,
            )
