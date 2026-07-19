"""Unit coverage for the one-way source-ban circuit breaker (FR-10.3, AC-72, ADR-004)."""

from __future__ import annotations

import pytest

from events_concierge.adapters.mock.policy import MockSourceQuarantineRepository
from events_concierge.domain.enums import Modality, Source
from events_concierge.domain.policy import SourcePolicy, SourceQuarantineSignal


async def test_mock_source_quarantine_is_monotonic_and_idempotent() -> None:
    """A crash/retry cannot reopen a source or rewrite its unrelated policy controls."""
    policy = SourcePolicy(
        source=Source.LUMA,
        automation_allowed={Modality.BROWSER: True, Modality.API: False},
        paid_allowed=True,
        signed_agent_mode="present-if-honored",
    )
    repository = MockSourceQuarantineRepository({Source.LUMA: policy})

    first = await repository.quarantine(Source.LUMA, SourceQuarantineSignal.FORBIDDEN)
    second = await repository.quarantine(Source.LUMA, SourceQuarantineSignal.BAN)

    assert first.newly_quarantined is True
    assert second.newly_quarantined is False
    assert policy.quarantined is True
    assert policy.automation_allowed == {Modality.BROWSER: True, Modality.API: False}
    assert policy.paid_allowed is True
    assert policy.signed_agent_mode == "present-if-honored"
    assert repository.calls == [
        (Source.LUMA, SourceQuarantineSignal.FORBIDDEN),
        (Source.LUMA, SourceQuarantineSignal.BAN),
    ]


async def test_mock_source_quarantine_fails_safe_when_policy_row_is_missing() -> None:
    """A missing durable control row is never silently synthesized as an allow."""
    repository = MockSourceQuarantineRepository({})

    with pytest.raises(RuntimeError, match="policy row is missing"):
        await repository.quarantine(Source.LUMA, SourceQuarantineSignal.FORBIDDEN)


async def test_mock_source_quarantine_surfaces_store_loss_for_handoff() -> None:
    """The application must see actuator loss and route safely instead of retrying the source."""
    repository = MockSourceQuarantineRepository({Source.LUMA: SourcePolicy(source=Source.LUMA)})
    repository.unavailable = True

    with pytest.raises(RuntimeError, match="store unavailable"):
        await repository.quarantine(Source.LUMA, SourceQuarantineSignal.BAN)
