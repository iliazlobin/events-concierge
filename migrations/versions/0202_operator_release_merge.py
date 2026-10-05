"""Join the retained operator schema and the published application schema.

Revision ID: 0202
Revises: 0198, 0201

Each branch upgrades normally before this merge. Keep the applied revision IDs;
do not stamp a retained database past either branch.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0202"
down_revision: tuple[str, str] = ("0198", "0201")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
