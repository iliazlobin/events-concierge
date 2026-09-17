"""Operator and executor startup isolation: credentials, database grants and catalog-only graph."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from events_concierge import catalog_runtime
from events_concierge.composition import Container
from events_concierge.config import Settings
from events_concierge.deployment import gcp_runtime
from events_concierge.infra.operator_database import (
    validate_operator_database_url,
    verify_database_role,
)
from events_concierge.workers import ingestion_cadence, ingestion_commands
from events_concierge.workflows import activities

_PRODUCTION_EXAMPLE = Path(__file__).resolve().parents[2] / "deployment/production.env.example"


def test_local_catalog_workflows_use_executor_profile_and_the_command_queue() -> None:
    """A combined tenant worker cannot execute the catalog driver's payloads or window grants."""
    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())
    services = compose["services"]
    catalog = services["catalog-workflow-worker"]["environment"]
    commands = services["ingestion-commands"]["environment"]
    transactional = services["workflow-worker"]["environment"]
    assert catalog["EC_TEMPORAL_WORKER_ROLE"] == "catalog"
    assert catalog["EC_INGESTION_EXECUTOR_ENABLED"] == "true"
    assert catalog["EC_INGESTION_EXECUTOR_DATABASE_URL"] == commands["EC_INGESTION_EXECUTOR_DATABASE_URL"]
    assert catalog["EC_TEMPORAL_TASK_QUEUE"] == commands["EC_TEMPORAL_CATALOG_TASK_QUEUE"]
    assert transactional["EC_TEMPORAL_WORKER_ROLE"] == "transactional"
    assert catalog["EC_TEMPORAL_TASK_QUEUE"] != transactional["EC_TEMPORAL_TRANSACTIONAL_TASK_QUEUE"]


@pytest.mark.parametrize(
    "url",
    [
        None,
        "postgresql+psycopg://ec_app:secret@db.example/ec?sslmode=verify-full",
        "postgresql+psycopg://postgres:secret@db.example/ec?sslmode=verify-full",
        "postgresql+psycopg://operator:secret@db.example/ec",
        "postgresql+psycopg://operator:secret@db.example/ec?sslmode=disable",
        "postgresql+psycopg://operator:secret@db.example/ec?sslmode=verify-full&options=-crole=postgres",
    ],
)
def test_operator_dsn_rejects_consumer_owner_and_unsafe_transport_without_echoing_secrets(
    url: str | None,
) -> None:
    with pytest.raises(ValueError) as caught:
        validate_operator_database_url(Settings(_env_file=None, mock_cloud=False), url)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "change",
    [
        {"rolsuper": True},
        {"rolbypassrls": True},
        {"rolcreaterole": True},
        {"rolcreatedb": True},
        {"permitted": False},
        {"consumer_role": True},
        {"role_name": "ec_app"},
        {"rolreplication": True},
        {"elevated_membership": True},
        {"database_owner": True},
        {"opposite_role": True},
        {"aggregate_role": True},
    ],
)
async def test_actual_database_privileges_are_verified(change: dict[str, object]) -> None:
    row: dict[str, object] = {
        "role_name": "operator_login",
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "permitted": True,
        "consumer_role": False,
        "rolreplication": False,
        "elevated_membership": False,
        "database_owner": False,
        "opposite_role": False,
        "aggregate_role": False,
    }
    row.update(change)
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mappings=lambda: SimpleNamespace(one=lambda: row),
            )
        )
    )
    with pytest.raises(RuntimeError, match="isolated runtime role"):
        await verify_database_role(cast("AsyncSession", session), "ec_operator_controller")


def test_catalog_graph_never_constructs_consumer_provider_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []
    store = object()
    settings = Settings(
        _env_file=None,
        env="staging",
        mock_cloud=False,
        ingestion_executor_enabled=True,
        ingestion_executor_database_url="postgresql+psycopg://executor_login:secret@db.example/ec?sslmode=verify-full",
        # Deliberately impossible full-provider identity; catalog composition must not load it.
        runtime_provider_factory="does_not_exist:provider",
        google_calendar_enabled=True,
        gcs_claim_check_prefix="events-concierge/catalog/claim-check/v1",
    )
    monkeypatch.setattr(catalog_runtime, "init_engine", lambda url, **kwargs: urls.append(url))
    monkeypatch.setattr(catalog_runtime, "build_gcs_object_store", lambda value: store)
    container = catalog_runtime.build_catalog_container(settings)
    assert container.object_store is store
    assert urls == [settings.ingestion_executor_database_url]
    assert not hasattr(container, "calendar")
    assert not hasattr(container, "vault")
    assert not hasattr(container, "auth_context")


def test_catalog_graph_rejects_the_consumer_claim_check_root_before_storage_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("shared consumer storage must fail before composition")

    settings = Settings(
        _env_file=None,
        mock_cloud=False,
        ingestion_executor_enabled=True,
        ingestion_executor_database_url="postgresql+psycopg://executor_login:secret@db.example/ec?sslmode=verify-full",
    )
    monkeypatch.setattr(catalog_runtime, "init_engine", forbidden)
    monkeypatch.setattr(catalog_runtime, "build_gcs_object_store", forbidden)
    with pytest.raises(ValueError, match="separate events-concierge/catalog/"):
        catalog_runtime.build_catalog_container(settings)


def test_consumer_provider_graph_cannot_use_the_reserved_catalog_storage_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("consumer catalog storage must fail before provider construction")

    monkeypatch.setattr(gcp_runtime, "build_gcs_object_store", forbidden)
    settings = Settings(
        _env_file=None, mock_cloud=False,
        gcs_claim_check_prefix="events-concierge/catalog/claim-check/v1",
    )
    with pytest.raises(gcp_runtime.GcpRuntimeConfigurationError, match="reserved catalog prefix"):
        gcp_runtime.build_runtime_ports(settings)


async def test_executor_rejects_wrong_database_role_before_router_or_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=_PRODUCTION_EXAMPLE,
        mock_cloud=False,
        oidc_bff_enabled=False,
        oidc_client_secret=None,
        ingestion_executor_enabled=True,
        ingestion_executor_database_url="postgresql+psycopg://executor_login:secret@db.example/ec?sslmode=verify-full",
    )
    router = AsyncMock()
    dispose = AsyncMock()
    monkeypatch.setattr(ingestion_commands, "dispose_engine", dispose)
    monkeypatch.setattr(ingestion_commands, "get_settings", lambda: settings)
    monkeypatch.setattr(ingestion_commands, "build_catalog_container", lambda value: object())
    monkeypatch.setattr(
        ingestion_commands,
        "verify_catalog_executor_database",
        AsyncMock(side_effect=RuntimeError("wrong role")),
    )
    monkeypatch.setattr(ingestion_commands, "build_catalog_refresh_router", router)
    with pytest.raises(RuntimeError, match="wrong role"):
        await ingestion_commands.run_ingestion_commands()
    router.assert_not_awaited()
    dispose.assert_awaited_once()


async def test_production_cadence_once_enqueues_and_closes_without_provider_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        env="production",
        mock_cloud=False,
        release_revision="0123456789abcdef0123456789abcdef01234567",
        image_digest="sha256:" + "a" * 64,
        catalog_ingestion_scheduler_enabled=True,
        operator_database_url="postgresql+psycopg://operator_login:secret@db.example/ec?sslmode=verify-full",
    )
    database = SimpleNamespace(session_scope=object(), aclose=AsyncMock())
    repository = object()
    report = SimpleNamespace(outcome=SimpleNamespace(value="idle"), due_sources=0)
    scheduler = SimpleNamespace(schedule_once=AsyncMock(return_value=report))
    loop = AsyncMock()
    monkeypatch.setattr(ingestion_cadence, "get_settings", lambda: settings)
    monkeypatch.setattr(ingestion_cadence, "OperatorDatabase", lambda value: database)
    monkeypatch.setattr(
        ingestion_cadence, "PostgresIngestionAdminRepository", lambda *a, **k: repository
    )
    monkeypatch.setattr(ingestion_cadence, "IngestionCadenceScheduler", lambda *a, **k: scheduler)
    monkeypatch.setattr(ingestion_cadence, "_run_ingestion_cadence_loop", loop)
    await ingestion_cadence.run_ingestion_cadence(once=True)
    scheduler.schedule_once.assert_awaited_once()
    database.aclose.assert_awaited_once()
    loop.assert_not_awaited()


def test_catalog_activity_binding_has_one_authoritative_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activities, "_container", None)
    monkeypatch.setattr(activities, "_catalog_container", None)
    full = cast("Container", object())
    catalog = cast("catalog_runtime.CatalogContainer", object())
    replacement = cast("Container", object())
    activities.set_container(full)
    assert activities._require_catalog() is full
    # Existing test/runtime replacements of the full binding cannot leave a stale catalog copy.
    monkeypatch.setattr(activities, "_container", replacement)
    assert activities._require_catalog() is replacement
    activities.set_catalog_container(catalog)
    assert activities._container is None
    assert activities._require_catalog() is catalog
    activities.set_container(full)
    assert activities._catalog_container is None
    assert activities._require_catalog() is full
