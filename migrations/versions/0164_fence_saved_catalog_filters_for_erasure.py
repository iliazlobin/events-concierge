"""Bring saved catalog filters under the account-erasure write fence.

0158 added ``public.saved_catalog_filters`` without the fence. Every relation carrying a
``tenant_id`` must refuse writes while that tenant's erasure is in flight, or a request racing the
purge could re-create rows behind it -- which is precisely what
``test_write_fence_inventory_covers_every_tenant_bearing_relation`` exists to catch.

Separate from 0158 rather than an amendment of it because 0158 has already been applied.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0164"
down_revision: str | None = "0163"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Refuse saved-filter writes for a tenant whose erasure has begun."""
    op.execute(
        """
        CREATE TRIGGER tr_account_erasure_write_fence
        BEFORE INSERT OR UPDATE OR DELETE ON public.saved_catalog_filters
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_fence_account_erasure_write()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tr_account_erasure_write_fence ON public.saved_catalog_filters"
    )
