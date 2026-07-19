"""Production runtime provisioning and fail-closed composition tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from events_concierge.adapters.mock.auth import HeaderAuthContext
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.mock.notifier import MockNotifier
from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.adapters.mock.vault import MockVault
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.runtime import RuntimePorts, load_runtime_ports


def _settings(**overrides: object) -> Settings:
    return Settings(
        discovery_sources="",
        mock_cloud=False,
        public_base_url="https://events.example.test",
        **overrides,
    )


def _runtime(tmp_path: Path, **overrides: object) -> RuntimePorts:
    values: dict[str, object] = {
        "auth_context": HeaderAuthContext(),
        "object_store": MockFilesystemObjectStore(tmp_path),
        "notifier": MockNotifier(),
        "notification_secret_protector": DevelopmentNotificationSecretProtector(),
        "credential_vault": MockVault(),
        "calendar": MockCalendar(),
        "register_sources": {},
        "withdrawal_sources": {},
    }
    values.update(overrides)
    return RuntimePorts(**values)


def _without_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _: None)


def test_runtime_provider_factory_supplies_non_mock_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The configured callable receives Settings and provisions every production-only boundary."""
    _without_engine(monkeypatch)
    module_name = "events_concierge_test_runtime_provider"
    module = ModuleType(module_name)
    runtime = _runtime(tmp_path)
    seen: list[Settings] = []

    def provide(settings: Settings) -> RuntimePorts:
        seen.append(settings)
        return runtime

    module.__dict__["provide"] = provide
    monkeypatch.setitem(sys.modules, module_name, module)
    settings = _settings(runtime_provider_factory=f"{module_name}:provide")

    container = build_container(settings)

    assert seen == [settings]
    assert container.auth_context is runtime.auth_context
    assert container.object_store is runtime.object_store
    assert container.notifier is runtime.notifier
    assert container.notification_secret_protector is runtime.notification_secret_protector
    assert container.vault is runtime.credential_vault
    assert container.calendar is runtime.calendar


def test_non_mock_composition_requires_https_user_capability_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A production notification must never carry a plaintext-HTTP completion capability."""
    _without_engine(monkeypatch)

    with pytest.raises(ValueError, match="require a public HTTPS base URL"):
        build_container(
            Settings(
                discovery_sources="",
                mock_cloud=False,
                public_base_url="http://events.example.test",
            ),
            runtime_ports=_runtime(tmp_path),
        )


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        (RuntimePorts(), "CalendarPort"),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
            ),
            "AuthContextPort",
        ),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
                auth_context=HeaderAuthContext(),
            ),
            "NotificationPort",
        ),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
                auth_context=HeaderAuthContext(),
                notifier=MockNotifier(),
            ),
            "NotificationSecretProtector",
        ),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
                auth_context=HeaderAuthContext(),
                notifier=MockNotifier(),
                notification_secret_protector=DevelopmentNotificationSecretProtector(),
            ),
            "CredentialVault",
        ),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
                auth_context=HeaderAuthContext(),
                notifier=MockNotifier(),
                notification_secret_protector=DevelopmentNotificationSecretProtector(),
                credential_vault=MockVault(),
            ),
            "register source map",
        ),
        (
            RuntimePorts(
                calendar=MockCalendar(),
                object_store=MockFilesystemObjectStore(Path("/tmp/runtime-test-store")),
                auth_context=HeaderAuthContext(),
                notifier=MockNotifier(),
                notification_secret_protector=DevelopmentNotificationSecretProtector(),
                credential_vault=MockVault(),
                register_sources={},
            ),
            "withdrawal source map",
        ),
    ],
)
def test_non_mock_composition_fails_closed_for_missing_runtime_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    runtime: RuntimePorts,
    message: str,
) -> None:
    _without_engine(monkeypatch)

    with pytest.raises(ValueError, match=message):
        build_container(_settings(), runtime_ports=runtime)


@pytest.mark.parametrize(
    "factory",
    [
        "missing-separator",
        "missing.module:provide",
    ],
)
def test_runtime_provider_rejects_unloadable_configuration(factory: str) -> None:
    with pytest.raises(ValueError, match="runtime_provider_factory"):
        load_runtime_ports(_settings(runtime_provider_factory=factory))


def test_runtime_provider_requires_a_runtime_ports_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "events_concierge_test_invalid_runtime_provider"
    module = ModuleType(module_name)
    module.__dict__["provide"] = lambda settings: settings
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(ValueError, match="must return RuntimePorts"):
        load_runtime_ports(_settings(runtime_provider_factory=f"{module_name}:provide"))
