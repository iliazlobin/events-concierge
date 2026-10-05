"""Resume daily cadence after a successful publication newer than a parked failure.

Revision ID: 0206
Revises: 0205

Failed manual attempts retain the cadence circuit break. A newer successful
publication establishes a new due slot without deleting failure or budget history.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0206"
down_revision: str | None = "0205"
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
    # Ignore whitespace without accepting a different expression or multiple gates.
    before_pattern = re.compile(r"\s+".join(re.escape(token) for token in before.split()))
    after_pattern = re.compile(r"\s+".join(re.escape(token) for token in after.split()))
    matches = tuple(before_pattern.finditer(definition))
    if len(matches) != 1:
        raise RuntimeError("cadence failure gate changed; review success-recovery migration")
    match = matches[0]
    if any(
        existing.start() < match.start() or existing.end() > match.end()
        for existing in after_pattern.finditer(definition)
    ):
        raise RuntimeError("cadence failure gate changed; review success-recovery migration")
    op.execute(definition[: match.start()] + after + definition[match.end() :])


def upgrade() -> None:
    _replace_gate(_FAILED_GATE, _RECOVERED_GATE)


def downgrade() -> None:
    _replace_gate(_RECOVERED_GATE, _FAILED_GATE)
