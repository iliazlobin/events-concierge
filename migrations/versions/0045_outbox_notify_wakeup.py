"""Wake the ADR-009 outbox relay after committed outbox inserts.

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Notify the relay only for outbox rows that survive their enclosing transaction."""
    op.execute(
        """
        CREATE FUNCTION public.fn_notify_outbox_ready()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            -- PostgreSQL publishes NOTIFY only when the enclosing transaction commits.  The fixed,
            -- empty payload avoids leaking tenant or event data and lets both direct INSERTs and the
            -- SECURITY DEFINER fn_transition INSERT wake the same relay path (ADR-009, FR-8.9).
            PERFORM pg_notify('ec_outbox_ready', '');
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_notify_outbox_ready() FROM PUBLIC")
    # A statement trigger coalesces bulk direct writes and guarded lifecycle-transition writes while
    # retaining PostgreSQL's transaction-commit notification semantics.
    op.execute(
        """
        CREATE TRIGGER tr_outbox_notify_ready
        AFTER INSERT ON public.outbox
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.fn_notify_outbox_ready()
        """
    )


def downgrade() -> None:
    """Remove the advisory wake-up path; durable relay polling remains available."""
    op.execute("DROP TRIGGER IF EXISTS tr_outbox_notify_ready ON public.outbox")
    op.execute("DROP FUNCTION IF EXISTS public.fn_notify_outbox_ready()")
