"""Fail-closed configuration contracts for the local ingestion control room."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from events_concierge.config import Settings


def test_ingestion_admin_is_disabled_by_default_and_bounded_when_local() -> None:
    assert Settings().admin_ingestion_enabled is False

    settings = Settings(
        admin_ingestion_enabled=True,
        catalog_ingestion_command_batch_size=10,
        catalog_ingestion_command_poll_seconds=0.25,
        catalog_ingestion_command_lease_seconds=21_600,
    )

    assert settings.mock_cloud is True
    assert settings.admin_ingestion_enabled is True
    assert settings.catalog_ingestion_command_batch_size == 10


def test_ingestion_admin_cannot_be_enabled_in_a_non_mock_deployment() -> None:
    with pytest.raises(ValidationError, match="ingestion admin is local-only"):
        Settings(mock_cloud=False, admin_ingestion_enabled=True)


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "192.168.1.10", "api.internal"])
def test_ingestion_admin_requires_a_loopback_edge_bind(address: str) -> None:
    with pytest.raises(ValidationError, match="loopback-only API bind"):
        Settings(admin_ingestion_enabled=True, api_bind_address=address)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "localhost"])
def test_ingestion_admin_accepts_explicit_loopback_edges(address: str) -> None:
    settings = Settings(admin_ingestion_enabled=True, api_bind_address=address)

    assert settings.api_bind_address == address


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog_ingestion_command_batch_size", 0),
        ("catalog_ingestion_command_batch_size", 11),
        ("catalog_ingestion_command_poll_seconds", 0.1),
        ("catalog_ingestion_command_poll_seconds", 61),
        ("catalog_ingestion_command_lease_seconds", 299),
        ("catalog_ingestion_command_lease_seconds", 21_601),
    ],
)
def test_ingestion_admin_worker_bounds_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})
