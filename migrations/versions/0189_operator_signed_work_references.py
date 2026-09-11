"""Allow canonical signed bigint references in notification record lookups.

Revision ID: 0189
Revises: 0188

The notification outbox uses PostgreSQL bigint IDs, including retained negative
references. Preserve the projection, owner and ACL while correcting only reference
validation. No work rows are changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from runpy import run_path

from alembic import op
from sqlalchemy import text

revision: str = "0189"
down_revision: str | None = "0188"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFINER = "ec_operator_aggregate_definer"
# Derive both definitions from the immutable predecessor, preserving all field
# redaction and scope predicates. CREATE OR REPLACE retains its owner and ACL.
DOWNGRADE_SQL: str = run_path(str(Path(__file__).with_name("0188_operator_work_records.py")))[
    "_PROJECTION"
].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
UPGRADE_SQL = DOWNGRADE_SQL.replace(
    "p_record_id !~ '^[1-9][0-9]{0,18}$'",
    "p_record_id !~ '^(0|-?[1-9][0-9]{0,18})$'",
    1,
).replace(
    "p_record_id::numeric > 9223372036854775807",
    "p_record_id::numeric NOT BETWEEN -9223372036854775808 AND 9223372036854775807",
    1,
)


def _replace_projection(definition: str) -> None:
    # A managed-service migration owner may have CREATEROLE without superuser or
    # effective definer membership. Borrow only the existing definer for this DDL.
    bind = op.get_bind()
    owner = bind.dialect.identifier_preparer.quote(
        bind.execute(text("SELECT current_user")).scalar_one()
    )
    op.execute(f"GRANT {_DEFINER} TO {owner}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_DEFINER}")
    op.execute(f"SET LOCAL ROLE {_DEFINER}")
    op.execute(definition)
    op.execute(f"SET LOCAL ROLE {owner}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_DEFINER}")
    op.execute(f"REVOKE {_DEFINER} FROM {owner}")


def upgrade() -> None:
    _replace_projection(UPGRADE_SQL)


def downgrade() -> None:
    _replace_projection(DOWNGRADE_SQL)
