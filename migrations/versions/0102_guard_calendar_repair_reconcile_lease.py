"""Expose a current repair-lease projection before direct calendar reconciliation.

Revision ID: 0102
Revises: 0101
Create Date: 2026-07-18

P22 guards repair terminal writes, but a worker may lose its repair lease after claim and before
the direct reconciliation path reaches CalendarPort. This fixed-shape, database-clock projection
locks only the exact repair row so a worker can skip an already stale/reclaimed repair before that
effect boundary (FR-8.7/8.7a, NFR-8, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0102"
down_revision: str | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_has_live_calendar_repair_lease(bigint, text)"


def upgrade() -> None:
    """Grant the app a narrow exact-repair lease projection, not queue-row access."""
    op.execute(
        """
        CREATE FUNCTION public.fn_has_live_calendar_repair_lease(
            p_repair_id bigint,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_repair public.event_change_calendar_repairs%ROWTYPE;
        BEGIN
            IF p_repair_id IS NULL
               OR p_repair_id < 1
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN false;
            END IF;

            SELECT repair.*
            INTO v_repair
            FROM public.event_change_calendar_repairs AS repair
            WHERE repair.repair_id = p_repair_id
            FOR UPDATE;
            IF NOT FOUND
               OR v_repair.repaired_at IS NOT NULL
               OR v_repair.lease_token IS DISTINCT FROM p_lease_token
               OR v_repair.lease_expires_at IS NULL
               OR v_repair.lease_expires_at <= pg_catalog.clock_timestamp()
            THEN
                RETURN false;
            END IF;
            RETURN true;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_app")


def downgrade() -> None:
    """Remove only the pre-reconciliation lease projection."""
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
