"""PostgreSQL immutable registration-action audit adapter (FR-7.3, NFR-8/10)."""

from __future__ import annotations

from sqlalchemy import text

from ...domain.audit import RegistrationActionAudit
from ...infra.db import tenant_session_scope


class PostgresRegistrationActionAuditRepository:
    """Append through PostgreSQL's sole guarded write path; direct table DML is unavailable."""

    async def append(self, record: RegistrationActionAudit) -> bool:
        """Append an immutable fact or acknowledge only an exact replay (NFR-8/10)."""
        async with tenant_session_scope(record.tenant_id) as session:
            appended = (
                await session.execute(
                    text(
                        """SELECT public.fn_append_registration_action_audit(
                               :audit_key,
                               :workflow_id,
                               :source,
                               :modality,
                               :phase,
                               :policy_decision,
                               :outcome,
                               :consent_ref
                           ) AS appended"""
                    ),
                    _parameters(record),
                )
            ).scalar_one()
        if not isinstance(appended, bool):
            raise RuntimeError("registration action-audit append returned a non-boolean result")
        return appended


def _parameters(record: RegistrationActionAudit) -> dict[str, object]:
    """Bind closed operational fields only; no event/provider/user content crosses this adapter."""
    return {
        "audit_key": record.audit_key,
        "workflow_id": record.workflow_id,
        "source": record.source.value,
        "modality": record.modality.value,
        "phase": record.phase.value,
        "policy_decision": record.policy_decision.value,
        "outcome": record.outcome.value if record.outcome is not None else None,
        "consent_ref": record.consent_ref,
    }
