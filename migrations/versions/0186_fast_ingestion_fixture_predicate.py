"""Avoid nested SQL interpretation in the per-run fixture predicate.

Revision ID: 0186
Revises: 0185

Source lists, contextual facets and other admin projections call this private helper
for every candidate refresh run. The original SQL helper calls the full error
normalizer through another function-local search_path boundary. A compiled predicate
can preserve the exact fixture decision without interpreting both SQL bodies for
each row. No function grant, signature, owner, data or public projection changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0186"
down_revision: str | None = "0185"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept as one canonical statement so a reviewed, narrowly targeted local repair can
# apply precisely this definition without falsely advancing the Alembic schema head.
# CREATE OR REPLACE retains the existing owner and ACL, including denied runtime access.
UPGRADE_SQL = r"""
CREATE OR REPLACE FUNCTION public.fn_ingestion_admin_run_is_fixture(
    p_run_key text,
    p_error text
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    -- fn_normalize_catalog_refresh_error checks this fixture branch before every
    -- nonempty error classification. Null/blank input cannot contain 'fixture'.
    RETURN COALESCE(
        p_run_key ~ '^manual:(p[0-9]+|paged-(stage|promotion)-contract-race-)', false
    ) OR COALESCE(lower(p_error) LIKE '%fixture%', false);
END;
$$
"""

# Exact original expression and SQL-language attributes from migration 0109.
DOWNGRADE_SQL = r"""
CREATE OR REPLACE FUNCTION public.fn_ingestion_admin_run_is_fixture(
    p_run_key text,
    p_error text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
    SELECT COALESCE(
        p_run_key ~ '^manual:(p[0-9]+|paged-(stage|promotion)-contract-race-)', false
    ) OR COALESCE(
        public.fn_normalize_catalog_refresh_error(p_error) = 'test_fixture', false
    )
$$
"""


def upgrade() -> None:
    """Replace only the private predicate, retaining all authority and null semantics."""
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    """Restore the original implementation without changing its grants."""
    op.execute(DOWNGRADE_SQL)
