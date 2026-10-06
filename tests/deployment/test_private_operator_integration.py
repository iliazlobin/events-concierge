"""Offline contracts for owner-authenticated operator access to the private runtime.

These renders use synthetic public metadata and never contact a cluster or provider.
Policy assertions evaluate the union of application and store NetworkPolicies so a
broader additive rule cannot silently restore operator access to unrelated services.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from tests.deployment.test_authenticated_private_runtime import (
    CHART,
    ROOT,
    _allows,
    render,
)
from tests.deployment.test_authenticated_private_runtime import (
    helm as helm,  # noqa: PLC0414 - expose the existing offline pytest fixture
)
from tests.deployment.test_authenticated_private_runtime import (
    values as runtime_values,
)

from events_concierge.config import Settings
from events_concierge.deployment.startup import preflight_operator_runtime

NAME = "events-concierge"
COMPONENT = "app.kubernetes.io/component"
OPERATOR_SUBJECT = "accounts.google.com:private-validation-owner"
OPERATOR_GSA = "ec-dev-operator-api@iz27-platform-dev.iam.gserviceaccount.com"


def operator_values() -> dict[str, Any]:
    result = runtime_values()
    result["operator"] = {
        "enabled": True,
        "hostname": "admin.concierge.example",
        "tlsSecretName": "validation-operator-tls",
        "iapAudience": "/projects/123456789/global/backendServices/987654321",
        "iapClientId": "validation.apps.googleusercontent.com",
        "iapClientSecretName": "validation-iap-oauth",
        "subjectRoles": {OPERATOR_SUBJECT: "reviewer"},
        "operatorSecrets": [
            {
                "fileName": "EC_OPERATOR_DATABASE_URL",
                "secretName": "ec-dev-operator-database-url",
                "version": "2",
            }
        ],
    }
    result["serviceAccounts"]["operator-api"] = {"gcpServiceAccount": OPERATOR_GSA}
    for name in ("operator-frontend", "operator-api"):
        result["workloads"][name]["enabled"] = True
    return result


def documents(result: subprocess.CompletedProcess[str]) -> dict[tuple[str, str], Any]:
    assert result.returncode == 0, result.stderr
    resources = {}
    for item in yaml.safe_load_all(result.stdout):
        if not item:
            continue
        key = (item["kind"], item["metadata"]["name"])
        assert key not in resources, f"duplicate rendered resource: {key}"
        resources[key] = item
    return resources


def resource(resources, kind, suffix):
    return resources[kind, NAME + "-" + suffix]


def pod(resources, suffix):
    return resource(resources, "Deployment", suffix)["spec"]["template"]["spec"]


def environment(container):
    return {item["name"]: item["value"] for item in container.get("env", [])}


@pytest.fixture(scope="module")
def resources(helm, tmp_path_factory):
    return documents(render(helm, tmp_path_factory.mktemp("private-operator"), operator_values()))


def test_private_operator_remains_off_without_an_explicit_complete_overlay(helm, tmp_path):
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    private = runtime_values()
    assert private.get("operator", {}).get("enabled", defaults["operator"]["enabled"]) is False
    for field in ("hostname", "tlsSecretName", "iapAudience", "iapClientId", "iapClientSecretName"):
        assert not private.get("operator", {}).get(field, defaults["operator"][field])
    assert not private.get("operator", {}).get("subjectRoles", defaults["operator"]["subjectRoles"])
    for name in ("operator-frontend", "operator-api"):
        assert private["workloads"][name]["enabled"] is False
    rendered = documents(render(helm, tmp_path, private))
    assert not any(
        "operator" in name
        for kind, name in rendered
        if kind
        in {"Deployment", "Service", "ConfigMap", "SecretProviderClass", "Gateway", "HTTPRoute"}
    )


def test_private_operator_configures_iap_without_provisioning_an_edge(resources):
    configured = operator_values()["operator"]
    frontend = environment(pod(resources, "operator-frontend")["containers"][0])
    assert frontend["EC_OPERATOR_API_ENABLED"] == "true"
    assert frontend["EC_OPERATOR_API_ORIGIN"] == "http://events-concierge-operator-api:8000"
    assert frontend["EC_OPERATOR_PUBLIC_ORIGIN"] == "https://" + configured["hostname"]
    config = resource(resources, "ConfigMap", "private-operator-api")["data"]
    assert config["EC_OPERATOR_API_ENABLED"] == "true"
    assert config["EC_OPERATOR_IAP_AUDIENCE"] == configured["iapAudience"]
    assert config["EC_OPERATOR_PUBLIC_ORIGIN"] == frontend["EC_OPERATOR_PUBLIC_ORIGIN"]
    assert json.loads(config["EC_OPERATOR_SUBJECT_ROLES"]) == {OPERATOR_SUBJECT: "reviewer"}
    assert all(
        item["spec"].get("type", "ClusterIP") == "ClusterIP"
        for (kind, _), item in resources.items()
        if kind == "Service"
    )
    assert not any(
        kind
        in {"Secret", "Ingress", "Gateway", "HTTPRoute", "GCPBackendPolicy", "HealthCheckPolicy"}
        for kind, _ in resources
    )
    consumer = environment(pod(resources, "frontend")["containers"][0])
    assert consumer["EC_OPERATOR_API_ENABLED"] == "false"


def test_operator_mounts_only_its_numbered_database_secret_and_postgres_ca(resources):
    api = pod(resources, "operator-api")
    assert "initContainers" not in api
    assert len(api["containers"]) == 1
    container = api["containers"][0]
    assert container["command"] == [
        "uvicorn",
        "events_concierge.api.operator:create_operator_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--no-server-header",
        "--no-access-log",
    ]
    assert container["envFrom"] == [{"configMapRef": {"name": NAME + "-private-operator-api"}}]
    assert (
        environment(container)["PGSSLROOTCERT"] == "/var/run/events-concierge-tls/postgres/ca.crt"
    )
    volumes = {volume["name"]: volume for volume in api["volumes"]}
    assert set(volumes) == {"tmp", "runtime-secrets", "postgres-ca"}
    assert volumes["postgres-ca"]["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
    assert volumes["postgres-ca"]["secret"]["secretName"] == "ec-dev-postgres-ca-v1"
    assert volumes["runtime-secrets"]["csi"]["volumeAttributes"]["secretProviderClass"] == (
        NAME + "-private-operator-api"
    )
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["postgres-ca"]["readOnly"] and mounts["runtime-secrets"]["readOnly"]
    files = yaml.safe_load(
        resource(resources, "SecretProviderClass", "private-operator-api")["spec"]["parameters"][
            "secrets"
        ]
    )
    assert files == [
        {
            "path": "EC_OPERATOR_DATABASE_URL",
            "resourceName": "projects/iz27-platform-dev/secrets/ec-dev-operator-database-url/versions/2",
        }
    ]
    config = resource(resources, "ConfigMap", "private-operator-api")["data"]
    assert config["EC_DATABASE_CONNECTION_MODE"] == "direct_tls"
    assert config["EC_IDENTITY_PLATFORM_ENABLED"] == "false"
    assert config["EC_ADMIN_INGESTION_ENABLED"] == config["EC_OIDC_BFF_ENABLED"] == "false"
    assert not any(
        key.startswith(("EC_REDIS_", "EC_TEMPORAL_", "EC_GCS_", "EC_SIGNUP_"))
        or key
        in {
            "EC_DATABASE_URL_FILE",
            "EC_MIGRATION_URL_FILE",
            "EC_INGESTION_EXECUTOR_DATABASE_URL_FILE",
        }
        or (key.startswith("EC_IDENTITY_PLATFORM_") and key != "EC_IDENTITY_PLATFORM_ENABLED")
        for key in config
    )
    assert ("ConfigMap", NAME + "-operator") not in resources
    assert ("ConfigMap", NAME + "-executor") not in resources


def test_operator_frontend_has_no_backend_identity_or_credentials(resources):
    frontend = pod(resources, "operator-frontend")
    assert frontend["automountServiceAccountToken"] is False
    assert "initContainers" not in frontend
    assert {volume["name"] for volume in frontend["volumes"]} == {"tmp"}
    assert "envFrom" not in frontend["containers"][0]
    api = pod(resources, "operator-api")
    assert api["automountServiceAccountToken"] is True
    assert api["serviceAccountName"] == NAME + "-operator-api"
    account = resource(resources, "ServiceAccount", "operator-api")
    assert account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == OPERATOR_GSA
    api_account_names = {
        pod(resources, name)["serviceAccountName"]
        for name in (
            "api",
            "account-erasure",
            "ingestion-executor",
            "temporal-catalog",
            "operator-api",
        )
    }
    assert len(api_account_names) == 5


def test_operator_fits_the_reviewed_singleton_capacity_without_proxy_surge(resources):
    for name, request in (
        ("operator-frontend", {"cpu": "25m", "memory": "128Mi"}),
        ("operator-api", {"cpu": "100m", "memory": "192Mi"}),
    ):
        deployment = resource(resources, "Deployment", name)
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"]["rollingUpdate"] == {
            "maxSurge": 0,
            "maxUnavailable": 1,
        }
        assert len(pod(resources, name)["containers"]) == 1
        assert pod(resources, name)["containers"][0]["resources"]["requests"] == request
        assert ("PodDisruptionBudget", NAME + "-" + name) not in resources


def test_rendered_operator_passes_real_startup_without_consumer_or_worker_credentials(resources):
    config = copy.deepcopy(resource(resources, "ConfigMap", "private-operator-api")["data"])
    del config["EC_OPERATOR_DATABASE_URL_FILE"]
    config["EC_OPERATOR_DATABASE_URL"] = (
        "postgresql+psycopg://ec_dev_operator:fixture@"
        "ec-dev-application-postgres.events-concierge-dev.svc.cluster.local/events?sslmode=verify-full"
    )
    config.update(environment(pod(resources, "operator-api")["containers"][0]))
    with patch.dict(os.environ, config, clear=True):
        settings = Settings(_env_file=None)
    assert settings.operator_api_enabled and not settings.identity_platform_enabled
    assert settings.operator_subject_roles == {OPERATOR_SUBJECT: "reviewer"}
    preflight_operator_runtime(settings)


def test_enabling_operator_preserves_private_executor_and_cadence_authority(
    helm, tmp_path, resources
):
    baseline = documents(render(helm, tmp_path, runtime_values()))
    for name in ("ingestion-executor", "temporal-catalog"):
        before, after = pod(baseline, name), pod(resources, name)
        assert before["serviceAccountName"] == after["serviceAccountName"]
        assert before["containers"] == after["containers"]
        assert before["volumes"] == after["volumes"]
        for kind in ("ConfigMap", "SecretProviderClass"):
            assert resource(baseline, kind, "private-" + name) == resource(
                resources, kind, "private-" + name
            )
    for kind, suffix in (
        ("CronJob", "ingestion-cadence"),
        ("ConfigMap", "private-controller"),
        ("SecretProviderClass", "private-controller"),
    ):
        assert resource(baseline, kind, suffix) == resource(resources, kind, suffix)


def test_operator_has_no_application_or_iap_route_during_migration(helm, tmp_path):
    config = operator_values()
    config["global"]["releasePhase"] = "migration"
    rendered = documents(render(helm, tmp_path, config))
    assert not any(
        kind in {"Deployment", "CronJob", "Gateway", "HTTPRoute", "GCPBackendPolicy"}
        for kind, _ in rendered
    )
    jobs = [item for (kind, _), item in rendered.items() if kind == "Job"]
    assert len(jobs) == 1
    assert {volume["name"] for volume in jobs[0]["spec"]["template"]["spec"]["volumes"]} == {
        "tmp",
        "migration-secrets",
        "postgres-ca",
    }


@pytest.mark.parametrize(
    "field",
    [
        "hostname",
        "tlsSecretName",
        "iapAudience",
        "iapClientId",
        "iapClientSecretName",
        "subjectRoles",
    ],
)
def test_incomplete_private_iap_configuration_fails_closed(helm, tmp_path, field):
    config = operator_values()
    config["operator"][field] = {} if field == "subjectRoles" else ""
    result = render(helm, tmp_path, config)
    assert result.returncode != 0, f"missing operator.{field} rendered"
    assert "operator" in result.stderr


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("workloads", "operator-api", "enabled"), False),
        (("workloads", "operator-frontend", "enabled"), False),
        (("workloads", "operator-api", "needsIdentity"), False),
        (("workloads", "operator-api", "runtimeSecrets"), False),
        (("workloads", "operator-api", "database"), False),
        (("workloads", "operator-api", "serviceAccount"), "api"),
        (("workloads", "operator-api", "operatorProfile"), "runtime"),
        (("workloads", "operator-api", "port"), 9000),
        (("workloads", "operator-frontend", "port"), 4000),
        (("workloads", "operator-api", "env"), {"EC_OPERATOR_API_ENABLED": "false"}),
        (
            ("workloads", "operator-api", "env"),
            {"EC_OPERATOR_API_ENABLED": "true", "EC_DATABASE_URL": "unauthorized"},
        ),
        (
            ("workloads", "operator-api", "command"),
            ["uvicorn", "events_concierge.api.app:create_app", "--factory"],
        ),
        (("workloads", "operator-frontend", "needsIdentity"), True),
        (("workloads", "operator-frontend", "runtimeSecrets"), True),
        (("workloads", "operator-frontend", "database"), True),
        (
            ("workloads", "operator-frontend", "env"),
            {"EC_OPERATOR_API_ORIGIN": "https://unauthorized.example"},
        ),
        (("workloads", "operator-frontend", "command"), ["node", "unreviewed.js"]),
        (("workloads", "operator-api", "replicas"), 2),
        (("workloads", "operator-frontend", "replicas"), 2),
        (("workloads", "operator-frontend", "resources", "requests", "cpu"), "100m"),
        (("workloads", "operator-api", "resources", "requests", "memory"), "512Mi"),
        (("workloads", "operator-api", "pdb", "enabled"), True),
        (("serviceAccounts", "operator-api", "gcpServiceAccount"), ""),
        (
            ("serviceAccounts", "operator-api", "gcpServiceAccount"),
            "ec-dev-api@iz27-platform-dev.iam.gserviceaccount.com",
        ),
        (
            ("serviceAccounts", "operator-api", "gcpServiceAccount"),
            "ec-dev-development-admin@iz27-platform-dev.iam.gserviceaccount.com",
        ),
        (("serviceAccounts", "operator-api", "name"), NAME + "-api"),
        (("serviceAccounts", "operator-api", "name"), ""),
        (("operator", "subjectRoles"), {"iliazlobin91@gmail.com": "reviewer"}),
        (("operator", "subjectRoles"), {OPERATOR_SUBJECT: "admin"}),
        (
            ("operator", "subjectRoles"),
            {OPERATOR_SUBJECT: "reviewer", "accounts.google.com:second-owner": "viewer"},
        ),
        (("networkPolicy", "enabled"), False),
        (("cloudSqlProxy", "enabled"), True),
        (("operator", "frontendIngressCidrs"), ["0.0.0.0/0"]),
    ],
)
def test_unsafe_private_operator_overrides_fail_before_rendering(helm, tmp_path, path, value):
    config = operator_values()
    node = config
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    result = render(helm, tmp_path, config)
    assert result.returncode != 0, f"unsafe {'.'.join(path)}={value!r} rendered"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "consumer",
        "executor",
        "redis",
        "temporal",
        "migration",
        "floating",
        "owner-name",
    ],
)
def test_private_operator_rejects_unpinned_or_non_operator_secrets(helm, tmp_path, mutation):
    config = operator_values()
    secrets = config["operator"]["operatorSecrets"]
    if mutation == "missing":
        secrets.clear()
    elif mutation == "duplicate":
        secrets.append(copy.deepcopy(secrets[0]))
    elif mutation == "floating":
        secrets[0]["version"] = "latest"
    elif mutation == "owner-name":
        secrets[0]["secretName"] = "ec-dev-migration-url"
    else:
        files = {
            "consumer": "EC_DATABASE_URL",
            "executor": "EC_INGESTION_EXECUTOR_DATABASE_URL",
            "redis": "EC_REDIS_URL",
            "temporal": "EC_TEMPORAL_API_KEY",
            "migration": "EC_MIGRATION_URL",
        }
        secrets.append({"fileName": files[mutation], "secretName": "unauthorized", "version": "2"})
    result = render(helm, tmp_path, config)
    assert result.returncode != 0, f"unsafe operator secret profile ({mutation}) rendered"


@pytest.fixture(scope="module")
def combined(helm, tmp_path_factory):
    directory = tmp_path_factory.mktemp("private-operator-policy")
    config = operator_values()
    config["publicTunnel"] = {"enabled": True, "tokenSecretName": "validation-consumer-tunnel"}
    app = render(helm, directory, config)
    application = documents(app)
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
        check=False,
        timeout=30,
    )
    stores = documents(data)
    assert not (application.keys() & stores.keys())
    return application | stores


def labels(resources, suffix):
    return resource(resources, "Deployment", suffix)["spec"]["template"]["metadata"]["labels"]


def test_combined_policies_admit_only_operator_proxy_and_application_postgres(combined):
    policies = [item for (kind, _), item in combined.items() if kind == "NetworkPolicy"]
    frontend, api = labels(combined, "operator-frontend"), labels(combined, "operator-api")
    postgres = combined["StatefulSet", "ec-dev-application-postgres"]["spec"]["template"][
        "metadata"
    ]["labels"]
    assert _allows(policies, frontend, "egress", 8000, peer_labels=api)
    assert _allows(policies, api, "ingress", 8000, peer_labels=frontend)
    assert _allows(policies, api, "egress", 5432, peer_labels=postgres)
    assert _allows(policies, postgres, "ingress", 5432, peer_labels=api)
    for address in ("130.211.0.1", "35.191.0.1"):
        assert _allows(policies, frontend, "ingress", 3000, address=address)
        assert not _allows(policies, api, "ingress", 8000, address=address)
    assert not _allows(policies, frontend, "ingress", 3000, address="203.0.113.10")
    assert _allows(policies, api, "egress", 80, address="169.254.169.254")
    assert _allows(policies, api, "egress", 443, address="203.0.113.10")
    for process in (frontend, api):
        for protocol in ("TCP", "UDP"):
            for dns in ("kube-dns", "node-local-dns"):
                assert _allows(
                    policies,
                    process,
                    "egress",
                    53,
                    peer_labels={"k8s-app": dns},
                    namespace="kube-system",
                    protocol=protocol,
                )


def test_additive_store_policies_cannot_restore_unrelated_operator_authority(combined):
    policies = [item for (kind, _), item in combined.items() if kind == "NetworkPolicy"]
    frontend, api = labels(combined, "operator-frontend"), labels(combined, "operator-api")
    consumer_api = labels(combined, "api")
    connector = labels(combined, "public-tunnel")
    for process in (frontend, api):
        for kind, name, port in (
            ("Deployment", "ec-dev-redis", 6379),
            ("StatefulSet", "ec-dev-temporal-postgres", 5432),
        ):
            store = combined[kind, name]["spec"]["template"]["metadata"]["labels"]
            assert not _allows(policies, process, "egress", port, peer_labels=store)
            assert not _allows(policies, store, "ingress", port, peer_labels=process)
        temporal = {
            "app.kubernetes.io/part-of": NAME,
            "app.kubernetes.io/component": "frontend",
            "app.kubernetes.io/name": "temporal",
        }
        assert not _allows(policies, process, "egress", 7233, peer_labels=temporal)
        assert not _allows(policies, process, "egress", 8000, peer_labels=consumer_api)
        for peer in (consumer_api, connector):
            assert not _allows(
                policies,
                process,
                "ingress",
                3000 if process == frontend else 8000,
                peer_labels=peer,
            )
    postgres = combined["StatefulSet", "ec-dev-application-postgres"]["spec"]["template"][
        "metadata"
    ]["labels"]
    assert not _allows(policies, frontend, "egress", 5432, peer_labels=postgres)
    assert not _allows(policies, postgres, "ingress", 5432, peer_labels=frontend)
    for address, port in (("169.254.169.254", 80), ("203.0.113.10", 443), ("10.40.0.10", 443)):
        assert not _allows(policies, frontend, "egress", port, address=address)
    assert not _allows(policies, api, "egress", 443, address="10.40.0.10")
    assert not _allows(policies, connector, "egress", 3000, peer_labels=frontend)
    assert not _allows(policies, connector, "egress", 8000, peer_labels=api)


def test_consumer_tunnel_filter_is_identical_when_operator_is_enabled(helm, tmp_path, combined):
    config = runtime_values()
    config["publicTunnel"] = {"enabled": True, "tokenSecretName": "validation-consumer-tunnel"}
    baseline = documents(render(helm, tmp_path, config))
    before = resource(baseline, "ConfigMap", "public-tunnel")["data"]
    after = resource(combined, "ConfigMap", "public-tunnel")["data"]
    assert before == after
    assert "-X-Goog-*" in after["Caddyfile"]
    assert "-Cf-Access-*" in after["Caddyfile"]
    assert "/admin" not in after["Caddyfile"]
    assert "operator" not in after["Caddyfile"]
