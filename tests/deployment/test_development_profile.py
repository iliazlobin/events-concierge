import importlib.util
import os
import pathlib
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
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

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

    def test_profile_rejects_other_namespaces_and_public_gateway(self):
        self.assertNotEqual(self.render("production").returncode, 0)
        self.assertNotEqual(self.render(extra=("--set", "gateway.enabled=true")).returncode, 0)

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

    def test_rendered_runtime_settings_load(self):

        docs = list(yaml.safe_load_all(self.render().stdout))
        config = next(d["data"] for d in docs if d and d["kind"] == "ConfigMap")
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
