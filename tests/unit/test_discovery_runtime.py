"""Real infrastructure composition with fail-closed deferred product capabilities."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.unit.test_gcp_runtime import _StorageClient
from tests.unit.test_production_config_validation import _production_settings, _surface

from events_concierge.adapters.disabled import (
    DisabledCalendar,
    DisabledCredentialVault,
    DisabledNotificationSecretProtector,
    DisabledNotifier,
    DisabledProductCapabilityError,
)
from events_concierge.adapters.gcs import GcsObjectStore
from events_concierge.adapters.oidc.session import OidcBffSessionAdapter
from events_concierge.adapters.policy.pacer import RedisPacer
from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.composition import build_container
from events_concierge.deployment.gcp_runtime import build_runtime_ports
from events_concierge.domain.credentials import Credential
from events_concierge.domain.enums import CredentialKind, Source
from events_concierge.operations.config_validation import validate_production_config
from events_concierge.ports.calendar import CalendarEntry
from events_concierge.ports.notifications import Notification, NotificationKind


async def test_non_mock_discovery_builds_real_infrastructure_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda *args, **kwargs: None)
    settings = _production_settings(release_profile="discovery")
    client = _StorageClient()
    runtime = build_runtime_ports(settings, storage_client=client)
    container = build_container(settings, runtime_ports=runtime)

    assert isinstance(container.catalog, PostgresCatalogRepository)
    assert isinstance(container.object_store, GcsObjectStore)
    assert isinstance(container.pacer, RedisPacer)
    assert isinstance(container.browser_session, OidcBffSessionAdapter)
    assert container.auth_context is container.browser_session
    assert container.csrf_protection is container.browser_session
    assert isinstance(container.calendar, DisabledCalendar)
    assert isinstance(container.notifier, DisabledNotifier)
    assert isinstance(container.notification_secret_protector, DisabledNotificationSecretProtector)
    assert isinstance(container.vault, DisabledCredentialVault)
    assert runtime.register_sources == runtime.withdrawal_sources == {}
    assert runtime.google_calendar_access is runtime.google_calendar_bindings is None
    assert client.bucket_calls == [] and client.list_calls == 0
    await container.browser_session.aclose()


async def test_every_disabled_product_operation_raises_including_reads_and_purges() -> None:
    tenant = uuid4()
    now = datetime.now(UTC)
    credential = Credential(
        uuid4(),
        tenant,
        Source.MEETUP,
        CredentialKind.OAUTH_REFRESH,
        b"opaque",
        "https://example.com",
    )
    entry = CalendarEntry("calendar-id", uuid4(), "Event", now, None, "UTC")
    notification = Notification(tenant, NotificationKind.COMPLETION, "Subject", "Body", "dedup")
    calendar, vault = DisabledCalendar(), DisabledCredentialVault()
    secret = DisabledNotificationSecretProtector()
    operations = [
        lambda: DisabledNotifier().send(notification),
        lambda: secret.protect_completion_url(tenant, "https://example.com/task"),
        lambda: secret.reveal_completion_url(tenant, "opaque"),
        lambda: vault.store(credential),
        lambda: vault.get(tenant, Source.MEETUP),
        lambda: vault.revoke(tenant, Source.MEETUP),
        lambda: vault.delete_tenant(tenant),
        lambda: calendar.free_busy(tenant, now, now),
        lambda: calendar.upsert_event(tenant, entry),
        lambda: calendar.delete_event(tenant, "calendar-id"),
        lambda: calendar.delete_tenant_events(tenant),
    ]
    for operation in operations:
        with pytest.raises(DisabledProductCapabilityError):
            await operation()


@pytest.mark.parametrize(
    "override",
    [
        {"agent_enabled": True},
        {"google_calendar_enabled": True},
        {"google_calendar_access_factory": "must.not.load:factory"},
        {"cohere_api_key": "must-not-use"},
    ],
)
def test_discovery_provider_configuration_fails_before_loading_external_factories(
    override: dict,
) -> None:
    settings = _production_settings(release_profile="discovery", **override)
    with pytest.raises(ValueError, match="discovery requires"):
        build_runtime_ports(settings, storage_client=_StorageClient())
    report = validate_production_config(settings, load_provider=False)
    assert any(
        check.name == "discovery_profile_configuration" and not check.passed
        for check in report.checks
    )


@pytest.mark.parametrize(
    "name",
    [
        "notifier",
        "notification_secret_protector",
        "credential_vault",
        "calendar",
        "register_sources",
        "withdrawal_sources",
        "google_calendar_access",
        "google_calendar_bindings",
    ],
)
def test_discovery_rejects_enabled_runtime_provider_overrides_before_database_initialization(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    def no_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("database initialization must follow discovery validation")

    monkeypatch.setattr("events_concierge.composition.init_engine", no_engine)
    settings = _production_settings(release_profile="discovery")
    runtime = build_runtime_ports(settings, storage_client=_StorageClient())
    override = (
        {Source.MEETUP: object()} if name.endswith("sources") else _surface("send", "get_access")
    )
    with pytest.raises(ValueError, match="disabled product ports"):
        build_container(settings, runtime_ports=replace(runtime, **{name: override}))
    # Explicit composition-root overrides cannot re-enable the same effects either.
    argument = "credential_vault" if name == "credential_vault" else name
    with pytest.raises(ValueError, match="disabled product ports"):
        build_container(settings, runtime_ports=runtime, **{argument: override})


def test_discovery_requires_oidc_instead_of_injected_header_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production_settings(release_profile="discovery", oidc_bff_enabled=False)
    runtime = build_runtime_ports(settings, storage_client=_StorageClient())
    with pytest.raises(ValueError, match="OIDC BFF as sole identity"):
        build_container(settings, runtime_ports=runtime, auth_context=_surface("resolve_tenant_id"))


def test_full_preflight_accepts_discovery_disablement_without_accepting_it_for_full_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production_settings(release_profile="discovery")
    runtime = build_runtime_ports(settings, storage_client=_StorageClient())
    monkeypatch.setattr(
        "events_concierge.operations.config_validation.load_runtime_ports", lambda _: runtime
    )
    report = validate_production_config(settings)
    assert report.passed
    assert not report.to_dict()["release_eligible"]
    assert any(
        check.name == "runtime_port_credential_vault" and "cleanup fail closed" in check.detail
        for check in report.checks
    )

    full = validate_production_config(settings.model_copy(update={"release_profile": "full"}))
    assert not full.passed
    assert {check.name for check in full.checks if not check.passed} == {
        "runtime_port_calendar",
        "runtime_port_notifier",
        "runtime_port_notification_secret_protector",
        "runtime_port_credential_vault",
    }


def test_discovery_preflight_keeps_tls_identity_and_shared_state_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production_settings(
        release_profile="discovery",
        temporal_tls_enabled=False,
        redis_url="redis://redis.internal.example/0",
    )
    runtime = build_runtime_ports(settings, storage_client=_StorageClient())
    monkeypatch.setattr(
        "events_concierge.operations.config_validation.load_runtime_ports", lambda _: runtime
    )
    report = validate_production_config(settings)
    assert not report.passed
    failed = {check.name for check in report.checks if not check.passed}
    assert "temporal_tls" in failed and "redis_tls" in failed
