"""Fail-closed coverage for the bounded GCP runtime provider."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from types import ModuleType, SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from events_concierge.adapters.gcs import GcsBlob, GcsBucket, GcsObjectStore, GcsStorageClient
from events_concierge.adapters.google_calendar.calendar import GoogleCalendarAdapter
from events_concierge.adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from events_concierge.adapters.postgres.calendar_bindings import PostgresGoogleCalendarBindings
from events_concierge.adapters.postgres.consent import (
    PostgresRegistrationConsentEvidenceRepository,
)
from events_concierge.config import Settings
from events_concierge.deployment.gcp_runtime import (
    GcpRuntimeConfigurationError,
    GcpRuntimeDependencyError,
    build_runtime_ports,
)
from events_concierge.domain.enums import Source


class _Blob:
    name = "unused"
    generation = 1

    def upload_from_string(self, data: bytes, *, if_generation_match: int) -> None:
        del data, if_generation_match

    def download_as_bytes(self) -> bytes:
        return b""

    def delete(self, *, if_generation_match: int) -> None:
        del if_generation_match


class _Bucket:
    def blob(self, blob_name: str) -> GcsBlob:
        del blob_name
        return _Blob()


class _StorageClient:
    def __init__(self) -> None:
        self.bucket_calls: list[str] = []
        self.list_calls = 0

    def bucket(self, bucket_name: str) -> GcsBucket:
        self.bucket_calls.append(bucket_name)
        return _Bucket()

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        del bucket, prefix, versions, page_size
        self.list_calls += 1
        return ()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mock_cloud": False,
        "gcs_claim_check_bucket": "private-claims",
        "gcs_claim_check_prefix": "events-concierge/claims/v1",
        "gcp_project": "events-staging",
        "discovery_sources": "public_jsonld",
        "crawl_user_agent": "events-concierge-test/1.0",
        "crawl_min_interval_ms": 1700,
        "crawl_seed_urls": "https://events.example.test/a, https://events.example.test/b",
        "google_calendar_enabled": False,
        "google_calendar_access_factory": None,
        "google_calendar_timeout_seconds": 3.0,
    }
    values.update(overrides)
    # The three GCP fields are added to Settings by the deployment-readiness parent slice.  A
    # namespace keeps this owned test hermetic while that cross-cutting edit lands concurrently.
    return cast("Settings", SimpleNamespace(**values))


def test_provider_builds_supported_ports_without_dependency_io() -> None:
    client = _StorageClient()

    runtime = build_runtime_ports(
        _settings(),
        storage_client=cast("GcsStorageClient", client),
    )

    assert isinstance(runtime.object_store, GcsObjectStore)
    assert client.bucket_calls == []
    assert client.list_calls == 0
    assert runtime.discovery_sources is not None
    assert [source.capability.source for source in runtime.discovery_sources] == [
        Source.PUBLIC_JSONLD
    ]
    assert runtime.register_sources == {}
    assert runtime.withdrawal_sources == {}
    assert isinstance(runtime.action_audit, PostgresRegistrationActionAuditRepository)
    assert isinstance(
        runtime.registration_consent,
        PostgresRegistrationConsentEvidenceRepository,
    )
    assert runtime.notifier is None
    assert runtime.notification_secret_protector is None
    assert runtime.credential_vault is None
    assert runtime.calendar is None


def test_provider_makes_disabled_discovery_and_mutation_lanes_explicit() -> None:
    runtime = build_runtime_ports(
        _settings(discovery_sources=""),
        storage_client=cast("GcsStorageClient", _StorageClient()),
    )

    assert runtime.discovery_sources == ()
    assert runtime.register_sources == {}
    assert runtime.withdrawal_sources == {}


def test_provider_rejects_mock_cloud_and_missing_or_ambiguous_gcs_settings() -> None:
    client = cast("GcsStorageClient", _StorageClient())
    with pytest.raises(GcpRuntimeConfigurationError, match="EC_MOCK_CLOUD=false"):
        build_runtime_ports(_settings(mock_cloud=True), storage_client=client)
    with pytest.raises(GcpRuntimeConfigurationError, match="EC_GCS_CLAIM_CHECK_BUCKET"):
        build_runtime_ports(_settings(gcs_claim_check_bucket=None), storage_client=client)
    with pytest.raises(GcpRuntimeConfigurationError, match="outer whitespace"):
        build_runtime_ports(
            _settings(gcs_claim_check_bucket=" private-claims"), storage_client=client
        )
    with pytest.raises(GcpRuntimeConfigurationError, match="EC_GCS_CLAIM_CHECK_PREFIX"):
        build_runtime_ports(_settings(gcs_claim_check_prefix=""), storage_client=client)
    with pytest.raises(GcpRuntimeConfigurationError, match="EC_GCP_PROJECT"):
        build_runtime_ports(_settings(gcp_project=" "), storage_client=client)


async def test_provider_lazily_imports_official_storage_client_and_passes_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StorageClient()
    constructor_calls: list[dict[str, object]] = []

    def construct(**kwargs: object) -> _StorageClient:
        constructor_calls.append(kwargs)
        return client

    module = SimpleNamespace(Client=construct)
    monkeypatch.setattr(
        "events_concierge.deployment.gcp_runtime.import_module",
        lambda name: module if name == "google.cloud.storage" else None,
    )

    runtime = build_runtime_ports(_settings())

    assert isinstance(runtime.object_store, GcsObjectStore)
    assert constructor_calls == []
    await runtime.object_store.delete_tenant(uuid4())
    assert constructor_calls == [{"project": "events-staging"}]
    assert client.list_calls == 2


async def test_provider_reports_missing_or_unusable_storage_dependency_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> object:
        raise ImportError("sensitive import detail")

    monkeypatch.setattr("events_concierge.deployment.gcp_runtime.import_module", missing)
    with pytest.raises(GcpRuntimeDependencyError, match="google-cloud-storage") as captured:
        build_runtime_ports(_settings())
    assert "sensitive" not in str(captured.value)

    monkeypatch.setattr(
        "events_concierge.deployment.gcp_runtime.import_module",
        lambda _: SimpleNamespace(Client=lambda **kwargs: object()),
    )
    with pytest.raises(GcpRuntimeDependencyError, match="required storage surface"):
        runtime = build_runtime_ports(_settings())
        assert runtime.object_store is not None
        await runtime.object_store.delete_tenant(uuid4())


def test_provider_builds_google_calendar_from_the_existing_access_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "events_concierge_test_gcp_calendar_access"
    module = ModuleType(module_name)
    access = SimpleNamespace(get_access=lambda tenant_id: tenant_id)
    module.__dict__["build_access"] = lambda: access
    monkeypatch.setitem(sys.modules, module_name, module)

    runtime = build_runtime_ports(
        _settings(
            google_calendar_enabled=True,
            google_calendar_access_factory=f"{module_name}:build_access",
        ),
        storage_client=cast("GcsStorageClient", _StorageClient()),
    )

    assert runtime.google_calendar_access is access
    assert isinstance(runtime.google_calendar_bindings, PostgresGoogleCalendarBindings)
    assert isinstance(runtime.calendar, GoogleCalendarAdapter)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (None, "EC_GOOGLE_CALENDAR_ACCESS_FACTORY"),
        ("malformed", "module:callable"),
        ("missing.module:factory", "could not be loaded"),
    ],
)
def test_provider_fails_closed_for_missing_or_unloadable_calendar_access(
    factory: str | None,
    message: str,
) -> None:
    with pytest.raises((GcpRuntimeConfigurationError, GcpRuntimeDependencyError), match=message):
        build_runtime_ports(
            _settings(
                google_calendar_enabled=True,
                google_calendar_access_factory=factory,
            ),
            storage_client=cast("GcsStorageClient", _StorageClient()),
        )


def test_provider_rejects_calendar_factory_with_the_wrong_port_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "events_concierge_test_bad_gcp_calendar_access"
    module = ModuleType(module_name)
    module.__dict__["build_access"] = object
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(GcpRuntimeDependencyError, match="did not return"):
        build_runtime_ports(
            _settings(
                google_calendar_enabled=True,
                google_calendar_access_factory=f"{module_name}:build_access",
            ),
            storage_client=cast("GcsStorageClient", _StorageClient()),
        )
