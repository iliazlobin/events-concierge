"""Join independent catalog filtering and Muse schema revisions.

Existing deployments can upgrade explicitly to 0210 for the catalog repair without
applying 0209. Enabling Muse still requires its schema and deployment authorization.
"""

from collections.abc import Sequence

revision: str = "0211"
down_revision: tuple[str, str] = ("0209", "0210")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
