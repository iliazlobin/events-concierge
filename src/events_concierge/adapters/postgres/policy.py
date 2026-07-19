"""PostgreSQL reader for the durable ADR-004 policy control plane."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ...domain.enums import Modality, Source
from ...domain.policy import (
    PolicyControlState,
    PolicySnapshot,
    SourcePolicy,
    SourceQuarantineResult,
    SourceQuarantineSignal,
)
from ...infra.db import system_session_scope, tenant_session_scope


class PostgresPolicySnapshotRepository:
    """Read a fresh source policy and global/per-tenant freeze under the app role.

    The durable store itself is mutated only by the owner-only policy-control functions installed
    in migration 0057.  A query failure intentionally propagates so ``StoreBackedPolicyEngine``
    turns it into a denial before any source mutation (FR-5.9, FR-7.2, ADR-004).
    """

    async def read_snapshot(self, tenant_id: UUID, source: Source) -> PolicySnapshot:
        """Load one PII-free action-boundary snapshot using the RLS tenant context."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT control.kill_switch AS global_kill_switch,
                                  COALESCE(tenant_control.kill_switch, false) AS tenant_kill_switch,
                                  source_policy.source,
                                  source_policy.automation_allowed,
                                  source_policy.paid_allowed,
                                  source_policy.quarantined,
                                  source_policy.signed_agent_mode
                           FROM public.policy_global_control AS control
                           LEFT JOIN public.tenant_policy_control AS tenant_control
                             ON tenant_control.tenant_id = :tenant_id
                           LEFT JOIN public.source_policy AS source_policy
                             ON source_policy.source = :source
                           WHERE control.singleton = true"""
                        ),
                        {"tenant_id": str(tenant_id), "source": source.value},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise RuntimeError("policy global control row is missing")
        control = _control_from_row(row)
        return PolicySnapshot(control=control, source_policy=_source_policy_from_row(row, source))

    async def read_control(self, tenant_id: UUID | None = None) -> PolicyControlState:
        """Load only the control flags, optionally under a tenant's RLS context."""
        if tenant_id is None:
            # A global policy-control lookup is intentionally tenant-neutral; it is not an
            # accidental RLS access with a missing tenant context (FR-1.4).
            async with system_session_scope() as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """SELECT control.kill_switch AS global_kill_switch,
                                      false AS tenant_kill_switch
                               FROM public.policy_global_control AS control
                               WHERE control.singleton = true"""
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        else:
            async with tenant_session_scope(tenant_id) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """SELECT control.kill_switch AS global_kill_switch,
                                      COALESCE(tenant_control.kill_switch, false)
                                          AS tenant_kill_switch
                               FROM public.policy_global_control AS control
                               LEFT JOIN public.tenant_policy_control AS tenant_control
                                 ON tenant_control.tenant_id = :tenant_id
                               WHERE control.singleton = true"""
                            ),
                            {"tenant_id": str(tenant_id)},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        if row is None:
            raise RuntimeError("policy global control row is missing")
        return _control_from_row(row)


class PostgresSourceQuarantineRepository:
    """Trip PostgreSQL's narrow, monotonic ban circuit breaker under the app role.

    `ec_app` may execute only `fn_quarantine_source`; it receives neither table DML nor an
    operation that can clear a quarantine or change any other source-policy field.  A database
    error propagates to the caller, which must route work to handoff rather than retrying the
    provider (FR-10.3, AC-72, ADR-004).
    """

    async def quarantine(
        self, source: Source, signal: SourceQuarantineSignal
    ) -> SourceQuarantineResult:
        """Atomically set a source quarantine, returning false only for an idempotent replay."""
        async with system_session_scope() as session:
            newly_quarantined = (
                await session.execute(
                    text(
                        """SELECT public.fn_quarantine_source(
                               :source,
                               :signal
                           ) AS newly_quarantined"""
                    ),
                    {"source": source.value, "signal": signal.value},
                )
            ).scalar_one()
        if not isinstance(newly_quarantined, bool):
            raise RuntimeError("source quarantine actuator returned a non-boolean result")
        return SourceQuarantineResult(
            source=source,
            signal=signal,
            newly_quarantined=newly_quarantined,
        )


def _control_from_row(row: RowMapping) -> PolicyControlState:
    """Validate closed control fields instead of trusting a malformed owner-side row."""
    global_kill_switch = row["global_kill_switch"]
    tenant_kill_switch = row["tenant_kill_switch"]
    if not isinstance(global_kill_switch, bool) or not isinstance(tenant_kill_switch, bool):
        raise RuntimeError("policy control row contains non-boolean kill-switch state")
    return PolicyControlState(
        global_kill_switch=global_kill_switch,
        tenant_kill_switch=tenant_kill_switch,
    )


def _source_policy_from_row(row: RowMapping, expected_source: Source) -> SourcePolicy | None:
    """Lift validated JSONB data into the closed domain source-policy shape."""
    source = row["source"]
    if source is None:
        return None
    if source != expected_source.value:
        raise RuntimeError("policy source row did not match the requested source")
    raw_allowed = row["automation_allowed"]
    paid_allowed = row["paid_allowed"]
    quarantined = row["quarantined"]
    signed_agent_mode = row["signed_agent_mode"]
    if not isinstance(raw_allowed, dict):
        raise RuntimeError("policy automation_allowed is not a JSON object")
    if not isinstance(paid_allowed, bool) or not isinstance(quarantined, bool):
        raise RuntimeError("policy source booleans are malformed")
    if signed_agent_mode not in {"none", "present-if-honored", "required"}:
        raise RuntimeError("policy signed-agent mode is malformed")

    automation_allowed: dict[Modality, bool] = {}
    for raw_modality, enabled in cast(dict[object, object], raw_allowed).items():
        if not isinstance(raw_modality, str) or not isinstance(enabled, bool):
            raise RuntimeError("policy automation_allowed contains a malformed entry")
        try:
            modality = Modality(raw_modality)
        except ValueError as error:
            raise RuntimeError("policy automation_allowed contains an unknown modality") from error
        automation_allowed[modality] = enabled
    return SourcePolicy(
        source=expected_source,
        automation_allowed=automation_allowed,
        paid_allowed=paid_allowed,
        quarantined=quarantined,
        signed_agent_mode=signed_agent_mode,
    )
