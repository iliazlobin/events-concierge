"""Secure shared Temporal client configuration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio.client import TLSConfig

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.config import Settings
from events_concierge.workflows.temporal_client import (
    connect_temporal,
    validate_temporal_settings,
)


class _FixtureClient:
    pass


async def test_local_temporal_connection_preserves_plaintext_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def connect(target: str, **kwargs: Any) -> _FixtureClient:
        captured["target"] = target
        captured.update(kwargs)
        return _FixtureClient()

    monkeypatch.setattr("events_concierge.workflows.temporal_client.Client.connect", connect)
    settings = Settings(
        temporal_target="localhost:7234",
        temporal_namespace="default",
        temporal_tls_enabled=False,
        temporal_api_key=None,
    )

    client = await connect_temporal(settings, MockFilesystemObjectStore(tmp_path))

    assert isinstance(client, _FixtureClient)
    assert captured["target"] == "localhost:7234"
    assert captured["namespace"] == "default"
    assert captured["api_key"] is None
    assert captured["tls"] is None
    assert captured["lazy"] is False
    assert captured["data_converter"] is not None


async def test_temporal_cloud_connection_uses_tls_domain_and_trimmed_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def connect(target: str, **kwargs: Any) -> _FixtureClient:
        captured["target"] = target
        captured.update(kwargs)
        return _FixtureClient()

    monkeypatch.setattr("events_concierge.workflows.temporal_client.Client.connect", connect)
    settings = Settings(
        temporal_target="example.tmprl.cloud:7233",
        temporal_namespace="production.example",
        temporal_tls_enabled=True,
        temporal_tls_domain="example.tmprl.cloud",
        temporal_api_key="  fixture-api-key  ",
    )

    await connect_temporal(settings, MockFilesystemObjectStore(tmp_path), lazy=True)

    assert captured["api_key"] == "fixture-api-key"
    assert captured["lazy"] is True
    tls = captured["tls"]
    assert isinstance(tls, TLSConfig)
    assert tls.domain == "example.tmprl.cloud"


async def test_eager_temporal_connection_has_the_configured_outer_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cancelled = False

    async def blocked_connect(*args: object, **kwargs: object) -> _FixtureClient:
        del args, kwargs
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True
        return _FixtureClient()

    monkeypatch.setattr(
        "events_concierge.workflows.temporal_client.Client.connect",
        blocked_connect,
    )

    with pytest.raises(TimeoutError):
        await connect_temporal(
            Settings(temporal_rpc_timeout_seconds=0.1),
            MockFilesystemObjectStore(tmp_path),
        )

    assert cancelled is True


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            Settings(temporal_api_key="fixture", temporal_tls_enabled=False),
            "requires TLS",
        ),
        (
            Settings(temporal_api_key=" \t", temporal_tls_enabled=True),
            "must not be empty",
        ),
        (
            Settings(
                temporal_tls_domain="example.tmprl.cloud",
                temporal_tls_enabled=False,
            ),
            "domain requires TLS",
        ),
        (
            Settings(temporal_tls_domain=" \t", temporal_tls_enabled=True),
            "domain must not be empty",
        ),
    ],
)
async def test_temporal_connection_rejects_insecure_or_blank_credentials(
    tmp_path: Path,
    settings: Settings,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await connect_temporal(settings, MockFilesystemObjectStore(tmp_path))


def test_temporal_validation_does_not_require_engine_reachability() -> None:
    settings = Settings(
        temporal_target="unreachable.example.test:7233",
        temporal_namespace="production.example",
        temporal_tls_enabled=True,
        temporal_tls_domain="unreachable.example.test",
        temporal_api_key="fixture-api-key",
    )

    validate_temporal_settings(settings)


def test_temporal_rpc_timeout_has_a_short_validated_bound() -> None:
    """A deployment cannot disable the outer Temporal call deadline with zero or an extreme value."""
    assert Settings().temporal_rpc_timeout_seconds == 5.0
    assert Settings(temporal_rpc_timeout_seconds=0.1).temporal_rpc_timeout_seconds == 0.1
    assert Settings(temporal_rpc_timeout_seconds=60.0).temporal_rpc_timeout_seconds == 60.0

    for invalid in (0.0, 0.099, -1.0, 1e-9, 60.01, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(temporal_rpc_timeout_seconds=invalid)
