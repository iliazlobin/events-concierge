"""Actual process entrypoints reject unsafe settings before readiness or dependency work."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock

import pytest

from events_concierge.api import app as api
from events_concierge.api import operator
from events_concierge.config import Settings
from events_concierge.deployment import startup
from events_concierge.workers import (
    account_erasure,
    catalog_refresh,
    catalog_refresh_dispatcher,
    change_detection,
    entity_intelligence,
    handoff_expiry,
    ingestion_cadence,
    ingestion_commands,
    lifecycle_invariants,
    notifier,
    request_starter,
)
from events_concierge.workflows import worker as temporal_worker

_EXAMPLE = Path(__file__).resolve().parents[2] / "deployment/production.env.example"
_EXECUTOR_URL = "postgresql+psycopg://executor_login:secret@db.example/ec?sslmode=verify-full"
_CONTROLLER_URL = "postgresql+psycopg://controller_login:secret@db.example/ec?sslmode=verify-full"


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=_EXAMPLE, **overrides)


@pytest.mark.parametrize(
    ("override", "check"),
    [
        (
            {"database_url": "postgresql+psycopg://ec_app:never-log@db.example/ec"},
            "application_database",
        ),
        ({"redis_url": "redis://:never-log@redis.example/0"}, "redis_tls"),
        ({"temporal_tls_enabled": False}, "temporal_tls"),
        ({"temporal_api_key": None}, "temporal_credentials"),
        ({"public_base_url": "https://localhost:14443"}, "public_origin"),
        ({"oidc_bff_enabled": False}, "production_identity_profile"),
        ({"mock_cloud": True, "oidc_bff_enabled": False}, "mock_cloud_disabled"),
        ({"release_revision": "development"}, "release_revision"),
        (
            {"migration_url": "postgresql://owner:never-log@db.example/ec"},
            "migration_credential_absent",
        ),
        ({"migration_url_file": "/not-mounted/never-open-this"}, "migration_credential_absent"),
    ],
)
async def test_api_never_enters_lifespan_or_builds_provider_with_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
    check: str,
) -> None:
    settings = _settings(**override)
    build = Mock()
    provider = Mock()
    connect = AsyncMock()
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "build_container", build)
    monkeypatch.setattr(api, "_configure_temporal", connect)
    monkeypatch.setattr(startup, "load_runtime_ports", provider)
    app = api.create_app()

    with pytest.raises(startup.RuntimePreflightError, match=check) as error:
        async with app.router.lifespan_context(app):
            pytest.fail("unsafe process reached readiness")

    assert "never-log" not in str(error.value)
    assert not hasattr(app.state, "container")
    build.assert_not_called()
    provider.assert_not_called()
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    ("module", "run"),
    [
        (account_erasure, account_erasure.run_account_erasure),
        (change_detection, change_detection.run_change_delivery),
        (entity_intelligence, entity_intelligence.run_entity_intelligence),
        (handoff_expiry, handoff_expiry.run_handoff_expiry),
        (lifecycle_invariants, lifecycle_invariants.run_lifecycle_invariants),
        (notifier, notifier.run_notifier),
        (request_starter, request_starter.run_request_starter),
        (temporal_worker, temporal_worker.run_worker),
    ],
)
@pytest.mark.parametrize(
    ("override", "check"),
    [
        ({"redis_url": "redis://redis.example/0"}, "redis_tls"),
        ({"oidc_bff_enabled": False}, "production_identity_profile"),
    ],
)
async def test_consumer_workers_cannot_exempt_transport_or_application_identity(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    run: Callable[[], Awaitable[object]],
    override: dict[str, object],
    check: str,
) -> None:
    settings = _settings(entity_intelligence_enabled=True, **override)
    build = Mock()
    provider = Mock()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "build_container", build)
    monkeypatch.setattr(startup, "load_runtime_ports", provider)
    with pytest.raises(startup.RuntimePreflightError, match=check):
        await run()
    build.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize(
    "module", [catalog_refresh, catalog_refresh_dispatcher, ingestion_commands, temporal_worker]
)
@pytest.mark.parametrize(
    ("override", "check"),
    [
        ({"redis_url": "redis://redis.example/0"}, "redis_tls"),
        ({"temporal_api_key": None}, "temporal_credentials"),
        (
            {
                "ingestion_executor_database_url": "postgresql+psycopg://executor:secret@db.example/ec"
            },
            "isolated_database_transport",
        ),
    ],
)
async def test_catalog_entrypoints_reject_insecure_dependencies_before_executor_work(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    override: dict[str, object],
    check: str,
) -> None:
    values: dict[str, object] = {
        "oidc_bff_enabled": False,
        "oidc_client_secret": None,
        "runtime_provider_factory": None,
        "ingestion_executor_enabled": True,
        "ingestion_executor_database_url": _EXECUTOR_URL,
        "temporal_worker_role": "catalog",
    }
    values.update(override)
    settings = _settings(**values)
    build = Mock()
    verify = AsyncMock()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "build_catalog_container", build)
    monkeypatch.setattr(module, "verify_catalog_executor_database", verify)
    with pytest.raises(startup.RuntimePreflightError, match=check):
        if module is catalog_refresh:
            await module.refresh_once("reviewed-source", "manual:preflight")
        elif module is catalog_refresh_dispatcher:
            await module.dispatch_once()
        elif module is ingestion_commands:
            await module.run_ingestion_commands()
        else:
            await temporal_worker.run_worker()
    build.assert_not_called()
    verify.assert_not_awaited()


def test_catalog_and_controller_checks_do_not_acquire_consumer_or_google_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock(
        side_effect=AssertionError("catalog/controller must not load application provider")
    )
    monkeypatch.setattr(startup, "load_runtime_ports", provider)
    catalog = _settings(
        oidc_bff_enabled=False,
        oidc_client_secret=None,
        runtime_provider_factory=None,
        ingestion_executor_enabled=True,
        ingestion_executor_database_url=_EXECUTOR_URL,
        database_url="unused-consumer-credential",
        media_backend="local",
        gcs_media_bucket=None,
    )
    startup.preflight_catalog_runtime(catalog)
    controller = Settings(
        _env_file=None,
        env="production",
        mock_cloud=False,
        operator_database_url=_CONTROLLER_URL,
        release_revision="0123456789abcdef0123456789abcdef01234567",
        image_digest="sha256:" + "a" * 64,
    )
    startup.preflight_operator_runtime(controller)
    provider.assert_not_called()


async def test_cadence_once_rejects_insecure_database_before_constructing_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        oidc_bff_enabled=False,
        oidc_client_secret=None,
        catalog_ingestion_scheduler_enabled=True,
        operator_database_url="postgresql+psycopg://controller:secret@db.example/ec",
    )
    database = Mock()
    monkeypatch.setattr(ingestion_cadence, "get_settings", lambda: settings)
    monkeypatch.setattr(ingestion_cadence, "OperatorDatabase", database)
    with pytest.raises(startup.RuntimePreflightError, match="isolated_database_transport"):
        await ingestion_cadence.run_ingestion_cadence(once=True)
    database.assert_not_called()


async def test_operator_api_preserves_and_rejects_migration_file_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        oidc_bff_enabled=False,
        operator_api_enabled=True,
        operator_iap_audience="/projects/123/global/backendServices/456",
        operator_policy_version="projects/123/locations/global/parameters/ec-operator-rbac/versions/release-1",
        operator_public_origin="https://ops.example.test",
        operator_database_url=_CONTROLLER_URL,
        migration_url_file="/not-mounted/never-open-this",
    )
    build = Mock()
    monkeypatch.setattr(operator, "build_operator_services", build)
    app = operator.create_operator_app(settings)
    with pytest.raises(startup.RuntimePreflightError, match="migration_credential_absent"):
        async with app.router.lifespan_context(app):
            pytest.fail("operator reached readiness with migration authority")
    build.assert_not_called()


def test_migration_credential_from_dotenv_is_detected_without_reading_owner_file(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(_EXAMPLE.read_text() + "\nEC_MIGRATION_URL_FILE=/not-mounted/owner-secret\n")
    settings = Settings(_env_file=dotenv)
    with pytest.raises(startup.RuntimePreflightError, match="migration_credential_absent"):
        startup.preflight_application_runtime(settings)
