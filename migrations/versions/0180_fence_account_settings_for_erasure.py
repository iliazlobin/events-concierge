"""Fence account-settings tables against writes during account erasure.

Revision ID: 0180
Revises: 0179
Create Date: 2026-09-05

Revisions 0157, 0160, 0161, and 0162 added tenant-bearing relations after the original account-
erasure fence inventory in 0108. Without the same trigger, a write racing the purge could recreate
profile, avatar-index, role, or API-key data after that relation had already been cleared.

Keep this repair separate from those applied revisions and attach the existing owner-controlled
fence function uniformly to every omitted relation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0180"
down_revision: str | None = "0179"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FENCED_TABLES = (
    "tenant_profiles",
    "tenant_profile_avatars",
    "tenant_roles",
    "tenant_api_keys",
)


def upgrade() -> None:
    """Refuse writes to account settings after tenant erasure begins."""
    for table in _FENCED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER tr_account_erasure_write_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW
            EXECUTE FUNCTION public.fn_fence_account_erasure_write()
            """
        )


def downgrade() -> None:
    """Remove the account-erasure write fences added by this revision."""
    for table in _FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS tr_account_erasure_write_fence ON public.{table}")
