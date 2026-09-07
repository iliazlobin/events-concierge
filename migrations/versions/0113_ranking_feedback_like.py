"""Add explicit positive preference feedback to the closed signal vocabulary.

Revision ID: 0113
Revises: 0112
Create Date: 2026-07-24

The consumer can now distinguish an explicit thumbs-up from implicit dwell and click signals.
Both ``like`` and ``click`` retain the same bounded positive influence in the initial ranking
baseline, so a downgrade can preserve the accumulated feature deltas by mapping ``like`` receipts
to the legacy ``click`` vocabulary before narrowing the database check.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0113"
down_revision: str | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNAL_KIND_CHECK = "tenant_ranking_feedback_receipts_signal_kind_check"


def upgrade() -> None:
    """Admit explicit likes without widening any other receipt authority."""
    op.execute(
        f"""
        ALTER TABLE public.tenant_ranking_feedback_receipts
        DROP CONSTRAINT {_SIGNAL_KIND_CHECK},
        ADD CONSTRAINT {_SIGNAL_KIND_CHECK}
            CHECK (signal_kind IN ('scroll', 'dwell', 'click', 'like', 'dismiss'))
        """
    )


def downgrade() -> None:
    """Map equivalent likes to clicks before restoring the legacy vocabulary."""
    op.execute(
        """
        UPDATE public.tenant_ranking_feedback_receipts
        SET signal_kind = 'click'
        WHERE signal_kind = 'like'
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.tenant_ranking_feedback_receipts
        DROP CONSTRAINT {_SIGNAL_KIND_CHECK},
        ADD CONSTRAINT {_SIGNAL_KIND_CHECK}
            CHECK (signal_kind IN ('scroll', 'dwell', 'click', 'dismiss'))
        """
    )
