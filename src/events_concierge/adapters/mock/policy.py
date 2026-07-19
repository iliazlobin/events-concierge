"""Deterministic policy-snapshot reader for offline PDP tests (ADR-004)."""

from __future__ import annotations

from uuid import UUID

from ...domain.enums import Source
from ...domain.policy import (
    PolicyControlState,
    PolicySnapshot,
    SourcePolicy,
    SourceQuarantineResult,
    SourceQuarantineSignal,
)


class MockPolicySnapshotReader:
    """Mutable fixture control plane that can model tenant/global flips and store loss.

    It deliberately returns a fresh snapshot on every call, matching the production reader's
    action-boundary semantics without relying on a process-local PDP policy map.
    """

    def __init__(
        self,
        source_policies: dict[Source, SourcePolicy] | None = None,
        *,
        global_kill_switch: bool = False,
    ) -> None:
        self.source_policies = source_policies or {}
        self.global_kill_switch = global_kill_switch
        self.tenant_kill_switches: dict[UUID, bool] = {}
        self.unavailable = False

    async def read_snapshot(self, tenant_id: UUID, source: Source) -> PolicySnapshot:
        """Return the current fixture snapshot or model a durable-store outage."""
        return PolicySnapshot(
            control=await self.read_control(tenant_id),
            source_policy=self.source_policies.get(source),
        )

    async def read_control(self, tenant_id: UUID | None = None) -> PolicyControlState:
        """Return the global plus optional tenant freeze; outages raise for PDP fail-closed logic."""
        if self.unavailable:
            raise RuntimeError("fixture policy store unavailable")
        return PolicyControlState(
            global_kill_switch=self.global_kill_switch,
            tenant_kill_switch=(
                self.tenant_kill_switches.get(tenant_id, False) if tenant_id is not None else False
            ),
        )


class MockSourceQuarantineRepository:
    """Faithful in-memory double for the monotonic source-ban circuit breaker.

    It shares a mutable source-policy map with `MockPolicySnapshotReader` so tests can prove a
    retry receives an idempotent receipt and the next fresh guard observes the quarantine without
    requiring PostgreSQL (FR-10.3, AC-72, ADR-004).
    """

    def __init__(self, source_policies: dict[Source, SourcePolicy]) -> None:
        self._source_policies = source_policies
        self.calls: list[tuple[Source, SourceQuarantineSignal]] = []
        self.unavailable = False

    async def quarantine(
        self, source: Source, signal: SourceQuarantineSignal
    ) -> SourceQuarantineResult:
        """Set only the quarantine bit or raise so callers take the fail-safe route."""
        if self.unavailable:
            raise RuntimeError("fixture source quarantine store unavailable")
        policy = self._source_policies.get(source)
        if policy is None:
            raise RuntimeError("fixture source policy row is missing")
        self.calls.append((source, signal))
        newly_quarantined = not policy.quarantined
        policy.quarantined = True
        return SourceQuarantineResult(
            source=source,
            signal=signal,
            newly_quarantined=newly_quarantined,
        )
