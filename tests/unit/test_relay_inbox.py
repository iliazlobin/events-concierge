"""RelayInbox opaque-secret contracts (FR-2.6, FR-5.7/5.8, ADR-011)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.email import MockEmailIngestion
from events_concierge.adapters.mock.vault import MockInjectionBroker
from events_concierge.domain.enums import Source
from events_concierge.ports.email import RelaySecretReference


def _reference(
    *,
    tenant_id: UUID | None = None,
    source: Source = Source.LUMA,
) -> RelaySecretReference:
    return RelaySecretReference(
        tenant_id=tenant_id or uuid4(),
        source=source,
        secret_ref=uuid4(),
        sender_domain="luma.com",
        expires_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sender_domain": "   "}, "sender_domain"),
        ({"expires_at": datetime(2026, 7, 17, 12, 0)}, "timezone-aware"),
        ({"secret_ref": cast(UUID, "not-a-uuid")}, "secret_ref"),
    ],
)
def test_relay_secret_reference_validates_opaque_capability_metadata(
    kwargs: dict[str, object], message: str
) -> None:
    """Only a UUID reference plus scoped, auditable metadata can cross the relay port (ADR-011)."""
    values: dict[str, object] = {
        "tenant_id": uuid4(),
        "source": Source.LUMA,
        "secret_ref": uuid4(),
        "sender_domain": "luma.com",
        "expires_at": datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        RelaySecretReference(**values)  # type: ignore[arg-type]

    assert {field.name for field in fields(RelaySecretReference)} == {
        "tenant_id",
        "source",
        "secret_ref",
        "sender_domain",
        "expires_at",
    }


@pytest.mark.asyncio
async def test_mock_relay_returns_only_a_matching_opaque_reference_or_none() -> None:
    """The offline relay never exposes code/link plaintext or another tenant's capability."""
    reference = _reference()
    relay = MockEmailIngestion(reference)

    assert (
        await relay.await_secret_reference(reference.tenant_id, reference.source, timeout_s=1.0)
    ) == reference
    assert not hasattr(reference, "otp")
    assert not hasattr(reference, "magic_link")
    assert await relay.await_secret_reference(uuid4(), reference.source, timeout_s=1.0) is None
    assert (
        await relay.await_secret_reference(reference.tenant_id, Source.MEETUP, timeout_s=1.0)
        is None
    )

    relay.set_secret_reference(None)
    assert (
        await relay.await_secret_reference(reference.tenant_id, reference.source, timeout_s=1.0)
        is None
    )


@pytest.mark.asyncio
async def test_injection_broker_accepts_and_records_only_an_opaque_reference() -> None:
    """Broker redemption receives the UUID capability, not an OTP/magic-link tool argument (ADR-011)."""
    reference = _reference()
    broker = MockInjectionBroker()

    assert await broker.fill_secret_reference(
        session_ref="browser-session",
        secret_ref=reference.secret_ref,
        field_ref="otp-field",
        bound_origin="https://luma.com",
    )
    assert broker.filled_secret_refs == [reference.secret_ref]

    assert not await broker.fill_secret_reference(
        session_ref="browser-session",
        secret_ref=uuid4(),
        field_ref="otp-field",
        bound_origin="",
    )
    assert broker.filled_secret_refs == [reference.secret_ref]
