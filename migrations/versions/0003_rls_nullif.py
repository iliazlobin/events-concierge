"""RLS policies fail closed on empty tenant context (NULLIF)

A custom GUC (app.tenant_id) reverts to the EMPTY STRING after a transaction-local set, not NULL, and
`''::uuid` raises instead of filtering. Wrapping current_setting in NULLIF(..., '') makes a missing or
empty context resolve to NULL, so the equality yields no rows -- fail closed (FR-1.4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("event_requests", "lifecycle", "transition_ledger", "handoff_tasks")
_NEW = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_OLD = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({_NEW}) WITH CHECK ({_NEW})")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({_OLD}) WITH CHECK ({_OLD})")
