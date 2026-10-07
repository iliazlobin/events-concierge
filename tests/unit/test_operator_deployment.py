"""Offline Helm contracts for the optional hosted operator deployment.

Run with Helm on PATH (or EC_HELM_BINARY). CI installs pinned tools and also validates
the rendered Kubernetes schemas. These checks never contact a cluster or a provider.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/events-concierge"
NAME = "events-concierge"
COMPONENT = "app.kubernetes.io/component"
OPERATOR_SUBJECT = "accounts.google.com:validation-subject"


def operator_values() -> dict[str, Any]:
    """Synthetic public metadata only; no credentials or live resource access."""
    return {
        "global": {"runtimeProviderReady": True},
        "operator": {
            "enabled": True,
            "hostname": "operator.concierge.example",
            "tlsSecretName": "validation-operator-tls",
            "iapAudience": "/projects/123456789/global/backendServices/987654321",
            "iapClientId": "validation.apps.googleusercontent.com",
            "iapClientSecretName": "validation-iap-oauth",
            "policyVersion": "projects/123456789/locations/global/parameters/ec-operator-rbac/versions/release-1",
        },
        "serviceAccounts": {
            "operator-api": {
                "gcpServiceAccount": "ec-controller@events-staging.iam.gserviceaccount.com",
            },
            "ingestion-executor": {
                "gcpServiceAccount": "ec-executor@events-staging.iam.gserviceaccount.com",
            },
        },
    }


@pytest.fixture(scope="module")
def helm_binary() -> str:
    binary = os.environ.get("EC_HELM_BINARY") or shutil.which("helm")
    if not binary:
        pytest.skip("Helm is optional locally; deployment-validation CI runs these contracts")
    return binary


def invoke_helm(
    binary: str, directory: Path, values: dict[str, Any], command: str = "template"
) -> subprocess.CompletedProcess[str]:
    override = directory / "values.json"
    override.write_text(json.dumps(values))
    args = [binary, command]
    if command == "template":
        args += [NAME]
    else:
        args += ["--strict"]
    args += [
        str(CHART),
        "--namespace",
        "events-concierge-staging",
        "--values",
        str(CHART / "values-staging.example.yaml"),
        "--values",
        str(override),
    ]
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=30)


@pytest.fixture(scope="module", params=[False, True], ids=["operator-disabled", "operator-enabled"])
def rendered(request: pytest.FixtureRequest, helm_binary: str, tmp_path_factory):
    enabled = request.param
    directory = tmp_path_factory.mktemp("operator-chart")
    values = operator_values() if enabled else {"global": {"runtimeProviderReady": True}}
    lint = invoke_helm(helm_binary, directory, values, "lint")
    assert lint.returncode == 0, lint.stdout + lint.stderr
    result = invoke_helm(helm_binary, directory, values)
    assert result.returncode == 0, result.stderr
    resources = {}
    for resource in yaml.safe_load_all(result.stdout):
        if resource is None:
            continue
        key = (resource["kind"], resource["metadata"]["name"])
        assert key not in resources, f"Duplicate rendered resource: {key}"
        resources[key] = resource
    if render_dir := os.environ.get("EC_DEPLOYMENT_RENDER_DIR"):
        destination = Path(render_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"operator-{'enabled' if enabled else 'disabled'}.yaml").write_text(
            result.stdout
        )
    return enabled, resources


def resource(resources, kind: str, suffix: str):
    return resources[(kind, f"{NAME}-{suffix}")]


def pod_spec(resources, suffix: str):
    return resource(resources, "Deployment", suffix)["spec"]["template"]["spec"]


def environment(container) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container.get("env", [])}


def secret_profile(pod) -> str | None:
    profiles = [
        volume["csi"]["volumeAttributes"]["secretProviderClass"]
        for volume in pod.get("volumes", [])
        if "csi" in volume
    ]
    assert len(profiles) <= 1
    return profiles[0] if profiles else None


def test_operator_resources_are_opt_in_and_never_expose_the_api(rendered):
    enabled, resources = rendered
    for kind, suffix in [
        ("Deployment", "operator-frontend"),
        ("Deployment", "operator-api"),
        ("Deployment", "ingestion-executor"),
        ("Gateway", "operator"),
        ("HTTPRoute", "operator"),
        ("GCPBackendPolicy", "operator-iap"),
        ("Service", "operator-frontend"),
        ("Service", "operator-api"),
        ("ConfigMap", "operator"),
        ("ConfigMap", "executor"),
    ]:
        assert ((kind, f"{NAME}-{suffix}") in resources) is enabled
    for (kind, _), item in resources.items():
        assert kind != "Secret", "Credentials must remain externally provisioned secret references"
        if kind == "Service":
            assert item["spec"].get("type", "ClusterIP") == "ClusterIP"
        if kind == "HTTPRoute":
            for rule in item["spec"]["rules"]:
                assert all("api" not in ref["name"] for ref in rule["backendRefs"])


def test_image_and_runtime_profiles_remain_separate(rendered):
    enabled, resources = rendered
    for (kind, _), item in resources.items():
        if kind != "Deployment":
            continue
        pod = item["spec"]["template"]["spec"]
        for container in pod["containers"] + pod.get("initContainers", []):
            assert re.search(r"@sha256:[0-9a-f]{64}$", container["image"])
        assert all(
            sidecar["restartPolicy"] == "Always" for sidecar in pod.get("initContainers", [])
        )
    assert secret_profile(pod_spec(resources, "api")) == f"{NAME}-runtime"
    catalog = pod_spec(resources, "temporal-catalog")
    assert secret_profile(catalog) == f"{NAME}-{'executor' if enabled else 'runtime'}"
    assert environment(catalog["containers"][0])["EC_TEMPORAL_WORKER_ROLE"] == "catalog"
    if not enabled:
        return
    operator = pod_spec(resources, "operator-api")
    executor = pod_spec(resources, "ingestion-executor")
    frontend = pod_spec(resources, "operator-frontend")
    assert not frontend["automountServiceAccountToken"]
    assert not frontend.get("initContainers")
    assert secret_profile(frontend) is None
    assert operator["automountServiceAccountToken"] and executor["automountServiceAccountToken"]
    assert (
        len(
            {
                operator["serviceAccountName"],
                executor["serviceAccountName"],
                pod_spec(resources, "api")["serviceAccountName"],
            }
        )
        == 3
    )
    assert catalog["serviceAccountName"] == executor["serviceAccountName"]
    for pod, profile in [(operator, "operator"), (executor, "executor"), (catalog, "executor")]:
        assert secret_profile(pod) == f"{NAME}-{profile}"
        assert pod["containers"][0]["envFrom"] == [{"configMapRef": {"name": f"{NAME}-{profile}"}}]
    assert (
        "events_concierge.api.operator:create_operator_app" in operator["containers"][0]["command"]
    )
    assert "events_concierge.workers.ingestion_commands" in executor["containers"][0]["command"]


def test_controller_cannot_mount_executor_or_consumer_credentials(rendered):
    enabled, resources = rendered
    if not enabled:
        return
    expected_files = {
        "operator": {"EC_OPERATOR_DATABASE_URL"},
        "executor": {
            "EC_INGESTION_EXECUTOR_DATABASE_URL",
            "EC_REDIS_URL",
            "REDIS_CA_CERTIFICATE",
            "EC_TEMPORAL_API_KEY",
        },
    }
    for profile, filenames in expected_files.items():
        csi = resource(resources, "SecretProviderClass", profile)
        secrets = yaml.safe_load(csi["spec"]["parameters"]["secrets"])
        assert {item["path"] for item in secrets} == filenames
        assert all(re.search(r"/versions/[1-9][0-9]*$", item["resourceName"]) for item in secrets)
        config = resource(resources, "ConfigMap", profile)["data"]
        assert config["EC_MOCK_CLOUD"] == "false"
        assert config["EC_ADMIN_INGESTION_ENABLED"] == "false"
        assert config["EC_OIDC_BFF_ENABLED"] == "false"
        assert "EC_DATABASE_URL_FILE" not in config
        assert "EC_OIDC_CLIENT_SECRET_FILE" not in config
        assert "EC_EMAIL_PROVIDER_TOKEN_FILE" not in config
    controller = resource(resources, "ConfigMap", "operator")["data"]
    executor = resource(resources, "ConfigMap", "executor")["data"]
    assert "EC_TEMPORAL_API_KEY_FILE" not in controller
    assert "EC_OPERATOR_DATABASE_URL_FILE" not in executor
    assert executor["EC_INGESTION_EXECUTOR_ENABLED"] == "true"
    assert executor["EC_GCS_CLAIM_CHECK_PREFIX"] == "events-concierge/catalog/claim-check/v1"
    assert (
        executor["EC_GCS_CLAIM_CHECK_PREFIX"]
        != resource(resources, "ConfigMap", "runtime")["data"]["EC_GCS_CLAIM_CHECK_PREFIX"]
    )
    assert controller["EC_OPERATOR_POLICY_VERSION"] == operator_values()["operator"]["policyVersion"]
    assert controller["EC_OPERATOR_POLICY_CACHE_SECONDS"] == "30"
    assert "EC_OPERATOR_SUBJECT_ROLES" not in controller


def test_iap_gateway_and_frontend_proxy_share_the_same_origin(rendered):
    enabled, resources = rendered
    consumer_env = environment(pod_spec(resources, "frontend")["containers"][0])
    assert consumer_env.get("EC_OPERATOR_API_ENABLED", "false") == "false"
    if not enabled:
        return
    configured = operator_values()["operator"]
    gateway = resource(resources, "Gateway", "operator")["spec"]["listeners"]
    assert len(gateway) == 1
    assert gateway[0]["protocol"] == "HTTPS" and gateway[0]["port"] == 443
    assert gateway[0]["hostname"] == configured["hostname"]
    assert gateway[0]["tls"]["certificateRefs"][0]["name"] == configured["tlsSecretName"]
    policy = resource(resources, "GCPBackendPolicy", "operator-iap")["spec"]
    assert policy["targetRef"]["name"] == f"{NAME}-operator-frontend"
    assert policy["default"]["iap"] == {
        "enabled": True,
        "clientID": configured["iapClientId"],
        "oauth2ClientSecret": {"name": configured["iapClientSecretName"]},
    }
    proxy_env = environment(pod_spec(resources, "operator-frontend")["containers"][0])
    assert proxy_env["EC_OPERATOR_API_ENABLED"] == "true"
    assert proxy_env["EC_OPERATOR_API_ORIGIN"] == f"http://{NAME}-operator-api:8000"
    assert proxy_env["EC_OPERATOR_PUBLIC_ORIGIN"] == f"https://{configured['hostname']}"
    config = resource(resources, "ConfigMap", "operator")["data"]
    assert config["EC_OPERATOR_PUBLIC_ORIGIN"] == proxy_env["EC_OPERATOR_PUBLIC_ORIGIN"]
    assert config["EC_OPERATOR_IAP_AUDIENCE"] == configured["iapAudience"]
    proxy_source = (ROOT / "web/app/admin/v1/[...path]/route.ts").read_text()
    for key in ["EC_OPERATOR_API_ENABLED", "EC_OPERATOR_API_ORIGIN", "EC_OPERATOR_PUBLIC_ORIGIN"]:
        assert f"process.env.{key}" in proxy_source, f"Chart/proxy environment drift: {key}"


def test_operator_api_network_ingress_is_only_proxy_and_private_monitoring(rendered):
    enabled, resources = rendered
    deny = resource(resources, "NetworkPolicy", "default-deny")["spec"]
    assert deny["podSelector"] == {}
    assert set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert not deny.get("ingress") and not deny.get("egress")
    if not enabled:
        return
    api_policy = resource(resources, "NetworkPolicy", "operator-api-ingress")["spec"]
    assert api_policy["podSelector"]["matchLabels"][COMPONENT] == "operator-api"
    allowed = []
    for rule in api_policy["ingress"]:
        assert rule["ports"] == [{"port": 8000, "protocol": "TCP"}]
        allowed.extend(rule["from"])
    assert len(allowed) == 2
    assert {
        peer["podSelector"]["matchLabels"][COMPONENT] for peer in allowed if "podSelector" in peer
    } == {"operator-frontend"}
    assert {
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for peer in allowed
        if "namespaceSelector" in peer
    } == {"gmp-system"}
    assert all("ipBlock" not in peer for peer in allowed)
    monitoring = resource(resources, "PodMonitoring", "operator-api")["spec"]
    assert monitoring["selector"]["matchLabels"][COMPONENT] == "operator-api"
    assert monitoring["endpoints"][0]["path"] == "/metrics"


def test_only_deployment_owned_cadence_can_enqueue_in_production(rendered):
    enabled, resources = rendered
    dispatcher = resource(resources, "CronJob", "catalog-dispatcher")["spec"]
    assert dispatcher["concurrencyPolicy"] == "Forbid"
    pod = dispatcher["jobTemplate"]["spec"]["template"]["spec"]
    command = pod["containers"][0]["command"]
    if enabled:
        assert command == ["python", "-m", "events_concierge.workers.ingestion_cadence", "--once"]
        assert secret_profile(pod) == f"{NAME}-operator"
        assert (
            pod["serviceAccountName"] == pod_spec(resources, "operator-api")["serviceAccountName"]
        )
        assert environment(pod["containers"][0])["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"] == "true"
    else:
        assert command == ["python", "-m", "events_concierge.workers.catalog_refresh_dispatcher"]
        assert secret_profile(pod) == f"{NAME}-runtime"
    for (kind, _), item in resources.items():
        if kind == "Deployment":
            for container in item["spec"]["template"]["spec"]["containers"]:
                assert "events_concierge.workers.ingestion_cadence" not in container.get(
                    "command", []
                )
        if not enabled:
            assert "events_concierge.workers.ingestion_commands" not in json.dumps(item)


@pytest.mark.parametrize(
    "field",
    [
        "hostname",
        "tlsSecretName",
        "iapAudience",
        "iapClientId",
        "iapClientSecretName",
        "policyVersion",
    ],
)
def test_operator_identity_configuration_fails_closed(helm_binary, tmp_path, field):
    values = operator_values()
    values["operator"][field] = ""
    result = invoke_helm(helm_binary, tmp_path, values)
    assert result.returncode != 0, f"Missing operator.{field} rendered successfully"
    assert f"operator.{field}" in result.stderr


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("networkPolicy", "enabled"), False),
        (("secretManager", "enabled"), False),
        (("cloudSqlProxy", "enabled"), False),
        (("global", "runtimeProviderReady"), False),
        (("workloads", "operator-api", "enabled"), False),
        (("workloads", "operator-frontend", "enabled"), False),
        (("workloads", "ingestion-executor", "enabled"), False),
        (("workloads", "temporal-catalog", "enabled"), False),
        (("workloads", "operator-api", "operatorProfile"), "runtime"),
        (("workloads", "ingestion-executor", "operatorProfile"), "operator"),
        (("workloads", "operator-api", "runtimeSecrets"), False),
        (("workloads", "operator-api", "serviceAccount"), "api"),
        (("workloads", "operator-frontend", "runtimeSecrets"), True),
        (("workloads", "operator-frontend", "needsIdentity"), True),
        (("serviceAccounts", "operator-api", "gcpServiceAccount"), ""),
        (("serviceAccounts", "ingestion-executor", "gcpServiceAccount"), ""),
        (
            ("serviceAccounts", "ingestion-executor", "gcpServiceAccount"),
            "ec-controller@events-staging.iam.gserviceaccount.com",
        ),
        (("serviceAccounts", "ingestion-executor", "name"), "events-concierge-operator-api"),
        (("serviceAccounts", "operator-api", "name"), "events-concierge-api"),
        (("serviceAccounts", "operator-api", "name"), ""),
        (("operator", "subjectRoles", OPERATOR_SUBJECT), "admin"),
        (("operator", "subjectRoles"), {OPERATOR_SUBJECT: "reviewer"}),
        (("operator", "policyVersion"), "projects/events-test/locations/global/parameters/ec-operator-rbac/versions/release-1"),
        (("operator", "policyVersion"), "projects/123456789/locations/global/parameters/ec-operator-rbac/versions/latest"),
        (("operator", "policyVersion"), "projects/123456789/locations/global/parameters/ec-operator-rbac/versions/LATEST"),
        (("operator", "policyVersion"), "projects/123456789/locations/global/parameters/-rbac/versions/release-1"),
        (("operator", "policyVersion"), "projects/123456789/locations/global/parameters/ec-operator-rbac/versions/-release"),
        (("operator", "policyVersion"), "projects/123456789/locations/us-west1/parameters/ec-operator-rbac/versions/release-1"),
        (("operator", "policyCacheSeconds"), 0),
        (("operator", "policyCacheSeconds"), 61),
        (("operator", "policyCacheSeconds"), 1.5),
        (("operator", "operatorSecrets"), []),
        (("operator", "executorSecrets"), []),
        (("operator", "executorClaimCheckPrefix"), "events-concierge/claim-check/v1"),
        (("applicationConfig", "EC_GCS_CLAIM_CHECK_PREFIX"), "events-concierge/catalog"),
        (
            ("applicationConfig", "EC_GCS_CLAIM_CHECK_PREFIX"),
            "events-concierge/catalog/claim-check/v1/tenant",
        ),
        (("jobs", "catalogRefresh", "enabled"), True),
    ],
)
def test_unsafe_operator_overrides_fail_render(helm_binary, tmp_path, path, value):
    values = operator_values()
    node = values
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    result = invoke_helm(helm_binary, tmp_path, values)
    assert result.returncode != 0, f"Unsafe {'.'.join(path)}={value!r} rendered successfully"


@pytest.mark.parametrize(
    ("profile", "mutation"),
    [
        ("operator", "missing-database"),
        ("executor", "missing-database"),
        ("operator", "consumer-credential"),
        ("executor", "consumer-credential"),
        ("operator", "floating-version"),
        ("executor", "floating-version"),
    ],
)
def test_credential_profiles_reject_missing_broad_or_unpinned_secrets(
    helm_binary, tmp_path, profile, mutation
):
    values = operator_values()
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    key = f"{profile}Secrets"
    secrets = copy.deepcopy(defaults["operator"][key])
    if mutation == "missing-database":
        secrets = [item for item in secrets if "DATABASE_URL" not in item["fileName"]]
    elif mutation == "consumer-credential":
        secrets.append({"fileName": "EC_DATABASE_URL", "secretName": "consumer-db", "version": "1"})
    else:
        secrets[0]["version"] = "latest"
    values["operator"][key] = secrets
    result = invoke_helm(helm_binary, tmp_path, values)
    assert result.returncode != 0, (
        f"Unsafe {profile} secret profile ({mutation}) rendered successfully"
    )


def test_managed_consumer_identity_does_not_mount_provider_secrets(helm_binary, tmp_path):
    values = yaml.safe_load((CHART / "values-consumer-identity.example.yaml").read_text())
    values["global"] = {"runtimeProviderReady": True}
    result = invoke_helm(helm_binary, tmp_path, values)
    assert result.returncode == 0, result.stderr
    resources = list(yaml.safe_load_all(result.stdout))
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    assert config["data"]["EC_IDENTITY_PLATFORM_ENABLED"] == "true"
    assert "EC_OIDC_CLIENT_SECRET_FILE" not in config["data"]
    secret_text = "\n".join(
        item["spec"]["parameters"]["secrets"]
        for item in resources if item["kind"] == "SecretProviderClass"
    )
    assert "EC_OIDC_CLIENT_SECRET" not in secret_text
    assert "ec-consumer-google" not in secret_text and "ec-consumer-apple" not in secret_text
    # ADC comes from the existing per-process Workload Identity accounts.
    for role in ("api", "account-erasure"):
        deployment = next(item for item in resources if item["kind"] == "Deployment"
                          and item["metadata"]["labels"][COMPONENT] == role)
        assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is True
