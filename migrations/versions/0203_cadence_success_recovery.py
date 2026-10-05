"""Resume daily cadence after a successful publication newer than a parked failure.

Revision ID: 0203
Revises: 0202

Failed manual attempts retain the cadence circuit break. A newer successful
publication establishes a new due slot without deleting failure or budget history.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0203"
down_revision: str | None = "0202"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAILED_GATE = "OR latest.attempt_status <> 'failed'"
_RECOVERED_GATE = (
    _FAILED_GATE
    + """
               OR candidate.last_succeeded_at > COALESCE(
                      latest.attempt_completed_at, latest.attempt_started_at
                  )"""
)


def _replace_gate(before: str, after: str) -> None:
    """Preserve the existing signature, eligibility, backoff, slot identity and ACL."""
    definition = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg_get_functiondef("
                "'public.fn_list_ingestion_admin_due_sources_v3(timestamptz,integer)'::regprocedure)"
            )
        )
        .scalar_one()
    )
    if definition.count(before) != 1 or after in definition.replace(before, "", 1):
        raise RuntimeError("cadence failure gate changed; review success-recovery migration")
    op.execute(definition.replace(before, after, 1))


def upgrade() -> None:
    _replace_gate(_FAILED_GATE, _RECOVERED_GATE)


def downgrade() -> None:
    _replace_gate(_RECOVERED_GATE, _FAILED_GATE)
