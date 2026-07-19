"""RLS-scoped Google app-calendar bindings.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Store only non-secret per-tenant calendar identifiers under FORCE RLS (FR-1.3/9.6)."""
    op.execute(
        """
        CREATE TABLE calendar_bindings (
            tenant_id              uuid PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            write_calendar_id      text NOT NULL,
            free_busy_calendar_ids jsonb NOT NULL,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now(),
            CHECK (
                jsonb_typeof(free_busy_calendar_ids) = 'array'
                AND jsonb_array_length(free_busy_calendar_ids) > 0
            )
        )
        """
    )
    op.execute("ALTER TABLE calendar_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE calendar_bindings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY tenant_isolation ON calendar_bindings
            USING ({_TENANT_POLICY}) WITH CHECK ({_TENANT_POLICY})"""
    )
    # The app role is non-owner and must be granted the same tenant-scoped DML as existing tables.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_bindings TO ec_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calendar_bindings")
