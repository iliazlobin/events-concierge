"""Recoverable registration list removal; keep attempts and provider evidence."""
from collections.abc import Sequence

from alembic import op

revision: str = "0222"
down_revision: str | None = "0221"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute("ALTER TABLE public.muse_signup_items ADD COLUMN removed_at timestamptz")

def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.muse_signup_items WHERE removed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'removed registrations exist; preserve their removal state';
            END IF;
        END $$;
        ALTER TABLE public.muse_signup_items DROP COLUMN removed_at;
    """)
