import importlib.util
import os
import pathlib
import shutil
import subprocess
import unittest
from unittest.mock import patch

import yaml

from events_concierge.adapters.gcs import GcsObjectStore
from events_concierge.config import Settings
from events_concierge.runtime import load_runtime_ports
from events_concierge.workflows.temporal_client import validate_temporal_settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELM = os.environ.get("HELM", "helm")
requires_helm = unittest.skipUnless(
    shutil.which(HELM),
    "Helm is optional locally; deployment-validation CI runs these contracts",
)
spec = importlib.util.spec_from_file_location(
    "dev_policy", ROOT / "scripts/development/check_plan.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class DevelopmentTests(unittest.TestCase):
    def render(self, namespace="events-concierge-dev", extra=()):
        return subprocess.run(
            [
                HELM,
                "template",
                "ec",
                str(ROOT / "deploy/helm/events-concierge"),
                "-n",
                namespace,
                "-f",
                str(ROOT / "deploy/helm/events-concierge/values-development.yaml"),
                "--set",
                "global.releasePhase=application",
                "--set",
                "global.runtimeProviderReady=true",
                "--set",
                "global.appImage.repository=test/app",
                "--set",
                "global.appImage.digest=sha256:" + "1" * 64,
                "--set",
                "global.frontendImage.repository=test/web",
                "--set",
                "global.frontendImage.digest=sha256:" + "1" * 64,
                "--set",
                "serviceAccounts.development-admin.gcpServiceAccount=admin@example.iam.gserviceaccount.com",
                "--set",
                "serviceAccounts.ingestion-executor.gcpServiceAccount=executor@example.iam.gserviceaccount.com",
                "--set",
                "serviceAccounts.temporal-catalog.gcpServiceAccount=catalog@example.iam.gserviceaccount.com",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    @requires_helm
    def test_development_is_private_single_replica_and_unscheduled(self):
        p = self.render()
        self.assertEqual(p.returncode, 0, p.stderr)
        docs = [d for d in yaml.safe_load_all(p.stdout) if d]
        self.assertTrue(any(d["kind"] == "Deployment" for d in docs))
        for d in docs:
            self.assertNotIn(
                d["kind"], {"CronJob", "HorizontalPodAutoscaler", "Gateway", "HTTPRoute"}
            )
            if d["kind"] == "Service":
                self.assertNotEqual(d["spec"].get("type"), "LoadBalancer")
            if d["kind"] == "Deployment":
                self.assertEqual(d["spec"]["replicas"], 1)
                self.assertFalse(d["spec"]["template"]["spec"].get("initContainers"))

    @requires_helm
    def test_admin_is_loopback_only_and_scheduler_stays_disabled(self):
        docs = [d for d in yaml.safe_load_all(self.render().stdout) if d]
        admin = next(
            d for d in docs if d.get("metadata", {}).get("name") == "events-concierge-admin"
        )
        containers = {c["name"]: c for c in admin["spec"]["template"]["spec"]["containers"]}
        api = containers["api"]
        env = {e["name"]: e["value"] for e in api["env"]}
        self.assertEqual(env["EC_ADMIN_INGESTION_ENABLED"], "true")
        self.assertEqual(env["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"], "false")
        self.assertEqual(api["command"][api["command"].index("--host") + 1], "127.0.0.1")
        web_env = {e["name"]: e["value"] for e in containers["frontend"]["env"]}
        self.assertEqual(web_env["HOSTNAME"], "127.0.0.1")
        self.assertFalse(
            any(
                d["kind"] == "Service"
                and d["spec"].get("selector", {}).get("app.kubernetes.io/name")
                == "events-concierge-admin"
                for d in docs
            )
        )

    @requires_helm
    def test_opt_in_development_cadence_only_queues_with_controller_credentials(self):
        result = self.render(extra=("--set", "developmentCatalog.cadenceEnabled=true"))
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        jobs = [d for d in docs if d["kind"] == "CronJob"]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "events-concierge-development-admin")
        process = pod["containers"][0]
        self.assertEqual(
            process["command"],
            ["python", "-m", "events_concierge.workers.ingestion_cadence", "--once"],
        )
        env = {item["name"]: item["value"] for item in process["env"]}
        self.assertEqual(env["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"], "true")
        self.assertIn("EC_OPERATOR_DATABASE_URL_FILE", env)
        self.assertNotIn("EC_INGESTION_EXECUTOR_DATABASE_URL_FILE", env)
        self.assertFalse(any(d["kind"] in {"Gateway", "HTTPRoute", "Ingress"} for d in docs))
        runtime = next(
            d
            for d in docs
            if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-runtime")
        )
        self.assertEqual(runtime["data"]["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"], "false")

    @requires_helm
    def test_development_cadence_refuses_missing_catalog_executor(self):
        result = self.render(
            extra=(
                "--set",
                "developmentCatalog.cadenceEnabled=true",
                "--set",
                "developmentCatalog.enabled=false",
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("development cadence requires", result.stderr)

    @requires_helm
    def test_controller_and_catalog_workers_have_separate_credentials(self):
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        docs = {d["metadata"]["name"]: d for d in yaml.safe_load_all(rendered.stdout) if d}
        admin = docs["events-concierge-admin"]["spec"]["template"]["spec"]
        self.assertEqual(admin["serviceAccountName"], "events-concierge-development-admin")
        admin_api = next(c for c in admin["containers"] if c["name"] == "api")
        self.assertIn(
            {
                "name": "EC_OPERATOR_DATABASE_URL_FILE",
                "value": "/var/run/secrets/events-concierge-operator/EC_OPERATOR_DATABASE_URL",
            },
            admin_api["env"],
        )
        # Consumer reads remain available to the combined local admin app; its controller
        # capability is a separate pool and a separately mounted credential.
        self.assertEqual(
            {
                v["csi"]["volumeAttributes"]["secretProviderClass"]
                for v in admin["volumes"]
                if "csi" in v
            },
            {"events-concierge-runtime", "events-concierge-development-operator"},
        )
        operator_secrets = yaml.safe_load(
            docs["events-concierge-development-operator"]["spec"]["parameters"]["secrets"]
        )
        self.assertEqual([s["path"] for s in operator_secrets], ["EC_OPERATOR_DATABASE_URL"])
        for name in ("ingestion-executor", "temporal-catalog"):
            pod = docs[f"events-concierge-{name}"]["spec"]["template"]["spec"]
            self.assertEqual(pod["serviceAccountName"], f"events-concierge-{name}")
            container = pod["containers"][0]
            self.assertEqual(
                container["envFrom"],
                [{"configMapRef": {"name": "events-concierge-development-executor"}}],
            )
            self.assertEqual(
                {
                    v["csi"]["volumeAttributes"]["secretProviderClass"]
                    for v in pod["volumes"]
                    if "csi" in v
                },
                {"events-concierge-development-executor"},
            )
        config = next(
            d["data"]
            for d in yaml.safe_load_all(rendered.stdout)
            if d
            and d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "events-concierge-development-executor"
        )
        self.assertEqual(config["EC_INGESTION_EXECUTOR_ENABLED"], "true")
        self.assertEqual(config["EC_GCS_CLAIM_CHECK_PREFIX"], "events-concierge/catalog/v1")
        self.assertEqual(config["EC_MOCK_CLOUD"], "true")
        self.assertEqual(config["EC_CATALOG_INGESTION_SCHEDULER_ENABLED"], "false")
        self.assertNotIn("EC_DATABASE_URL_FILE", config)
        self.assertNotIn("EC_OPERATOR_DATABASE_URL_FILE", config)
        self.assertNotIn("EC_TEMPORAL_API_KEY_FILE", config)
        executor_secrets = yaml.safe_load(
            docs["events-concierge-development-executor"]["spec"]["parameters"]["secrets"]
        )
        self.assertEqual(
            {s["path"] for s in executor_secrets},
            {"EC_INGESTION_EXECUTOR_DATABASE_URL", "EC_REDIS_URL"},
        )

    @requires_helm
    def test_development_migration_bootstraps_restricted_logins(self):
        rendered = self.render(extra=("--set", "global.releasePhase=migration"))
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
        job = next(d for d in docs if d["kind"] == "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["command"],
            [
                "python",
                "-m",
                "events_concierge.deployment.development_operator_bootstrap",
                "--migrate",
            ],
        )
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual(env["EC_ENV"], "development")
        self.assertEqual(env["EC_DATABASE_CONNECTION_MODE"], "development_plaintext")
        self.assertTrue(env["EC_DEV_OPERATOR_PASSWORD_FILE"].endswith("/EC_DEV_OPERATOR_PASSWORD"))
        self.assertTrue(
            env["EC_DEV_INGESTION_PASSWORD_FILE"].endswith("/EC_DEV_INGESTION_PASSWORD")
        )
        self.assertFalse(any(d["kind"] == "Deployment" for d in docs))

    @requires_helm
    def test_development_rejects_credential_and_identity_reuse(self):
        for override in (
            "global.deploymentProfile=managed",
            "serviceAccounts.development-admin.gcpServiceAccount=",
            "serviceAccounts.ingestion-executor.gcpServiceAccount=admin@example.iam.gserviceaccount.com",
            "serviceAccounts.temporal-catalog.name=events-concierge-api",
            "workloads.temporal-catalog.serviceAccount=api",
            "developmentCatalog.executorSecrets[0].secretName=ec-dev-database-url",
            "developmentAdmin.operatorSecrets[0].secretName=ec-dev-ingestion-executor-database-url",
            "applicationConfig.EC_GCS_CLAIM_CHECK_PREFIX=events-concierge/catalog",
            "applicationConfig.EC_MOCK_CLOUD=false",
            "applicationConfig.EC_GOOGLE_CALENDAR_ENABLED=true",
            "catalogDispatcher.enabled=true",
        ):
            with self.subTest(override=override):
                self.assertNotEqual(self.render(extra=("--set", override)).returncode, 0)

    @requires_helm
    def test_stores_have_retained_disks_and_recovery_probes(self):
        rendered = subprocess.run(
            [
                HELM,
                "template",
                "ec-dev-data",
                str(ROOT / "deploy/helm/events-concierge-dev-data"),
                "-n",
                "events-concierge-dev",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
        claims = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"}
        self.assertEqual(claims, {"ec-dev-application", "ec-dev-temporal", "ec-dev-redis"})
        storage = next(d for d in docs if d["kind"] == "StorageClass")
        self.assertEqual(storage["provisioner"], "pd.csi.storage.gke.io")
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        for d in docs:
            if d["kind"] not in {"Deployment", "StatefulSet"}:
                continue
            pod = d["spec"]["template"]["spec"]
            self.assertIn(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], claims)
            c = pod["containers"][0]
            self.assertTrue(c["volumeMounts"])
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                self.assertGreaterEqual(c[probe]["timeoutSeconds"], 5)
            if c["name"] == "redis":
                self.assertIn("--appendonly yes --appendfsync everysec", c["args"][0])
                self.assertEqual(c["volumeMounts"][0]["mountPath"], "/data")
                self.assertEqual(d["spec"]["strategy"]["type"], "Recreate")

    @requires_helm
    def test_profile_rejects_other_namespaces_and_public_gateway(self):
        self.assertNotEqual(self.render("production").returncode, 0)
        self.assertNotEqual(self.render(extra=("--set", "gateway.enabled=true")).returncode, 0)

    @requires_helm
    def test_development_rejects_hosted_operator_gateway(self):
        result = self.render(extra=("--set", "operator.enabled=true"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("development disables operator.enabled", result.stderr)

    def test_rejects_managed_services_and_destructive_plans(self):
        for kind, actions in [
            ("google_sql_database_instance", ["create"]),
            ("google_container_cluster", ["delete"]),
            ("google_redis_instance", ["create"]),
        ]:
            p = {
                "resource_changes": [
                    {"address": "bad", "type": kind, "change": {"actions": actions, "after": {}}}
                ]
            }
            self.assertTrue(policy.inspect(p))

    @requires_helm
    def test_shared_stores_reference_platform_storage_without_taking_ownership(self):
        rendered = subprocess.run(
            [
                HELM,
                "template",
                "ec-dev-data",
                str(ROOT / "deploy/helm/events-concierge-dev-data"),
                "-n",
                "events-concierge-dev",
                "--set",
                "createStorageClass=false",
                "--set",
                "storageClass=shared-retain",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        documents = [item for item in yaml.safe_load_all(rendered.stdout) if item]
        self.assertFalse(any(item["kind"] == "StorageClass" for item in documents))
        claims = [item for item in documents if item["kind"] == "PersistentVolumeClaim"]
        self.assertEqual(len(claims), 3)
        self.assertTrue(all(item["spec"]["storageClassName"] == "shared-retain" for item in claims))
        self.assertTrue(
            all(
                item["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
                for item in claims
            )
        )

    def test_development_plaintext_cannot_escape_test_profile(self):

        with self.assertRaises(ValueError):
            Settings(
                _env_file=None,
                env="production",
                mock_cloud=True,
                database_connection_mode="development_plaintext",
            )
        with self.assertRaises(ValueError):
            Settings(
                _env_file=None,
                env="development",
                mock_cloud=False,
                database_connection_mode="development_plaintext",
            )

    @requires_helm
    def test_rendered_runtime_settings_load(self):

        docs = list(yaml.safe_load_all(self.render().stdout))
        config = next(
            d["data"]
            for d in docs
            if d
            and d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "events-concierge-runtime"
        )
        self.assertNotIn("EC_TEMPORAL_API_KEY_FILE", config)
        self.assertNotIn("EC_OIDC_CLIENT_SECRET_FILE", config)
        # The two mounted store credentials are covered by secret-file unit tests.
        config.pop("EC_DATABASE_URL_FILE")
        config.pop("EC_REDIS_URL_FILE")
        config["EC_DATABASE_URL"] = (
            "postgresql+psycopg://ec_app:test@ec-dev-application-postgres:5432/events"
        )
        config["EC_REDIS_URL"] = "redis://:test@ec-dev-redis:6379/0"
        with patch.dict(os.environ, config, clear=True):
            settings = Settings(_env_file=None)
        validate_temporal_settings(settings)
        self.assertEqual(settings.env, "development")
        self.assertTrue(settings.mock_cloud)
        self.assertEqual(settings.temporal_worker_max_concurrent_workflow_tasks, 2)

    def test_real_storage_provider_loads_with_mock_product_adapters(self):
        settings = Settings(
            _env_file=None,
            env="development",
            mock_cloud=True,
            runtime_provider_factory="events_concierge.deployment.development_runtime:build_runtime_ports",
            gcp_project="project-9c8cce04-f94d-40fc-aa6",
            gcs_claim_check_bucket="iz27-ec-dev-payloads",
        )
        ports = load_runtime_ports(settings)
        self.assertIsInstance(ports.object_store, GcsObjectStore)
