"""Pinned Temporal chart contracts; rendering does not prove a live TLS handshake."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELM = os.environ.get("HELM", "helm")
DATABASE_HOST = "ec-dev-temporal-postgres.events-concierge-dev.svc.cluster.local"
FRONTEND_HOST = "ec-dev-temporal-frontend.events-concierge-dev.svc.cluster.local"
TLS_DIRECTORY = "/etc/temporal/private-tls"


@pytest.fixture(scope="module")
def rendered_temporal() -> list[dict[str, Any]]:
    chart_location = os.environ.get("EC_TEMPORAL_CHART_DIR")
    if chart_location is None:
        pytest.skip("set EC_TEMPORAL_CHART_DIR to the unpacked pinned Temporal 1.6.0 chart")
    assert shutil.which(HELM), "Helm is required when EC_TEMPORAL_CHART_DIR is set"
    chart = Path(chart_location)
    metadata = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert metadata["name"] == "temporal"
    assert metadata["version"] == "1.6.0"
    assert str(metadata["appVersion"]) == "1.31.2"
    result = subprocess.run(
        [
            HELM,
            "template",
            "ec-dev-temporal",
            str(chart),
            "--namespace",
            "events-concierge-dev",
            "--values",
            str(ROOT / "deploy/helm/temporal-development.yaml"),
            "--values",
            str(ROOT / "deploy/helm/temporal-private-tls.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [resource for resource in yaml.safe_load_all(result.stdout) if resource]


@pytest.fixture(scope="module")
def temporal_config(rendered_temporal: list[dict[str, Any]]) -> dict[str, Any]:
    configmap = next(
        resource
        for resource in rendered_temporal
        if resource["kind"] == "ConfigMap"
        and resource["metadata"]["name"] == "ec-dev-temporal-config"
    )
    # The server expands environment expressions on startup. Substitute only their scalar
    # placeholders to inspect the surrounding rendered config without retrieving any secrets.
    template = configmap["data"]["config_template.yaml"]
    return yaml.safe_load(re.sub(r"\{\{.*?\}\}", "RUNTIME_VALUE", template))


def test_pinned_temporal_render_requires_mutual_tls_and_verified_peer_names(
    temporal_config: dict[str, Any],
) -> None:
    tls = temporal_config["global"]["tls"]
    assert set(tls) == {"internode", "frontend"}
    for group, hostname in (
        ("internode", "ec-dev-temporal-internode"),
        ("frontend", FRONTEND_HOST),
    ):
        assert tls[group]["server"] == {
            "certFile": f"{TLS_DIRECTORY}/tls.crt",
            "keyFile": f"{TLS_DIRECTORY}/tls.key",
            "requireClientAuth": True,
            "clientCaFiles": [f"{TLS_DIRECTORY}/ca.crt"],
        }
        assert tls[group]["client"]["serverName"] == hostname
        assert tls[group]["client"]["rootCaFiles"] == [f"{TLS_DIRECTORY}/ca.crt"]
        assert not tls[group]["client"].get("disableHostVerification", False)


@pytest.mark.parametrize("store", ["default", "visibility"])
def test_temporal_postgres_verifies_the_connection_hostname(
    temporal_config: dict[str, Any], store: str
) -> None:
    sql = temporal_config["persistence"]["datastores"][store]["sql"]
    assert sql["pluginName"] == "postgres12"
    # Temporal's PostgreSQL driver validates its DSN host; TLS.serverName does not override it.
    assert sql["connectAddr"] == f"{DATABASE_HOST}:5432"
    assert sql["tls"]["enabled"] is True
    assert sql["tls"]["enableHostVerification"] is True
    assert sql["tls"]["caFile"] == "/etc/temporal/postgres-tls/ca.crt"
    assert sql["tls"]["serverName"] == DATABASE_HOST
    assert "sslmode" not in sql.get("connectAttributes", {})
    assert "sslrootcert" not in sql.get("connectAttributes", {})
    assert "keyFile" not in sql["tls"] and "certFile" not in sql["tls"]


def test_all_temporal_servers_can_read_only_the_intended_certificate_mounts(
    rendered_temporal: list[dict[str, Any]],
) -> None:
    deployments = [resource for resource in rendered_temporal if resource["kind"] == "Deployment"]
    assert {resource["metadata"]["name"] for resource in deployments} == {
        f"ec-dev-temporal-{service}" for service in ("frontend", "history", "matching", "worker")
    }
    for deployment in deployments:
        pod = deployment["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsUser"] == 1000
        assert pod["securityContext"]["fsGroup"] == 1000
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        assert volumes["temporal-private"]["secret"] == {
            "secretName": "ec-dev-temporal-tls-v1",
            "defaultMode": 0o440,
        }
        assert volumes["postgres-ca"]["secret"] == {
            "secretName": "ec-dev-postgres-ca-v1",
            "defaultMode": 0o444,
        }
        assert len(pod["containers"]) == 1
        container = pod["containers"][0]
        assert container["image"].startswith("temporalio/server:1.31.2@sha256:")
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        for name, directory in (
            ("temporal-private", TLS_DIRECTORY),
            ("postgres-ca", "/etc/temporal/postgres-tls"),
        ):
            assert mounts[name]["mountPath"] == directory
            assert mounts[name]["readOnly"] is True
            assert "subPath" not in mounts[name]


def test_restored_temporal_render_has_no_database_schema_or_namespace_mutation(
    rendered_temporal: list[dict[str, Any]],
) -> None:
    jobs = [resource for resource in rendered_temporal if resource["kind"] == "Job"]
    # Chart 1.6.0 retains an echo-only hook even when every schema operation is disabled.
    assert len(jobs) == 1
    job = jobs[0]
    assert job["metadata"]["name"] == "ec-dev-temporal-schema"
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    pod = job["spec"]["template"]["spec"]
    assert not pod.get("initContainers")
    assert len(pod["containers"]) == 1
    completion = pod["containers"][0]
    assert completion["command"] == ["sh", "-c", 'echo "Store setup completed"']
    assert not completion.get("env") and not completion.get("envFrom")
    assert not completion.get("volumeMounts") and not pod.get("volumes")
    assert not any(resource["kind"] == "Secret" for resource in rendered_temporal)


def test_temporal_services_remain_private(rendered_temporal: list[dict[str, Any]]) -> None:
    services = [resource for resource in rendered_temporal if resource["kind"] == "Service"]
    assert services
    assert all(resource["spec"].get("type", "ClusterIP") == "ClusterIP" for resource in services)
    assert not any(resource["kind"] in {"Ingress", "Gateway"} for resource in rendered_temporal)


def test_shared_store_overlay_matches_temporal_tls_hostname_without_owning_storage_class(
    temporal_config: dict[str, Any],
) -> None:
    chart = ROOT / "deploy/helm/events-concierge-dev-data"
    result = subprocess.run(
        [
            HELM,
            "template",
            "ec-dev-stores",
            str(chart),
            "--namespace",
            "events-concierge-dev",
            "--values",
            str(chart / "values-shared-development.yaml"),
            "--values",
            str(chart / "values-private-tls.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    resources = [resource for resource in yaml.safe_load_all(result.stdout) if resource]
    assert not any(resource["kind"] == "StorageClass" for resource in resources)
    claims = [resource for resource in resources if resource["kind"] == "PersistentVolumeClaim"]
    assert claims
    assert all(resource["spec"]["storageClassName"] == "shared-retain" for resource in claims)
    postgres = next(
        resource
        for resource in resources
        if resource["kind"] == "StatefulSet"
        and resource["metadata"]["name"] == "ec-dev-temporal-postgres"
    )["spec"]["template"]["spec"]["containers"][0]
    server_name = next(
        variable["value"]
        for variable in postgres["env"]
        if variable["name"] == "POSTGRES_TLS_SERVER_NAME"
    )
    assert "ssl=on" in postgres["args"]
    probe = " ".join(postgres["readinessProbe"]["exec"]["command"])
    assert "PGSSLMODE=verify-full" in probe and "$POSTGRES_TLS_SERVER_NAME" in probe
    for store in ("default", "visibility"):
        sql = temporal_config["persistence"]["datastores"][store]["sql"]
        assert server_name == sql["connectAddr"].rsplit(":", 1)[0] == DATABASE_HOST
