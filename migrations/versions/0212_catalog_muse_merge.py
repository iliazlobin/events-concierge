"""Join independent catalog filtering and Muse schema revisions.

Existing deployments can upgrade explicitly to 0211 for the catalog repair without
applying 0209 or 0210. Muse and catalog name suggestions require their own enablement.
"""

from collections.abc import Sequence

revision: str = "0212"
down_revision: tuple[str, str] = ("0210", "0211")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
