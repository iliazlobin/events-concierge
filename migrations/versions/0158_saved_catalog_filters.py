"""Let a tenant name and reuse a catalog filter selection.

Revision ID: 0158
Revises: 0157
Create Date: 2026-08-26

A saved filter is user-authored presentation state, not identity and not authority, so it lands in
its own FORCE-RLS satellite the application writes directly -- the same shape ``tenant_profiles``
uses, and for the same reason: ``public.tenants`` stays an insert-once read model.

The payload is stored as ``jsonb`` rather than one column per filter because the catalog filter set
is a client-side vocabulary that changes with the UI (it gained ``priceComparison`` and multi-source
selection in 0154's wake). Pinning that shape into columns would make every UI change a migration.
What the database does guarantee is that the payload is a bounded object and the name is a sane,
unique, printable label -- the parts that must hold no matter what the client sends.

``last_used_at`` is separate from ``updated_at`` on purpose: the picker sorts by recency of USE, and
applying a saved filter must not look like an edit of it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0158"
down_revision: str | None = "0157"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# A saved filter is a short label plus a filter set; anything larger is not a filter selection.
_MAX_PAYLOAD_BYTES = 8192


def upgrade() -> None:
    """Create a FORCE-RLS, tenant-writable store for named catalog filter selections."""
    op.execute(
        f"""
        CREATE TABLE public.saved_catalog_filters (
            saved_filter_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       uuid NOT NULL
                            REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            name            text NOT NULL,
            filters         jsonb NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_used_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT saved_catalog_filters_name_valid CHECK (
                name = btrim(name)
                AND char_length(name) BETWEEN 1 AND 80
                AND name !~ '[[:cntrl:]]'
            ),
            CONSTRAINT saved_catalog_filters_payload_is_object CHECK (
                jsonb_typeof(filters) = 'object'
            ),
            CONSTRAINT saved_catalog_filters_payload_bounded CHECK (
                pg_column_size(filters) <= {_MAX_PAYLOAD_BYTES}
            ),
            CONSTRAINT saved_catalog_filters_name_unique UNIQUE (tenant_id, name)
        )
        """
    )
    # The picker's default order. Ties break on the id so paging and rendering are deterministic.
    op.execute(
        """
        CREATE INDEX saved_catalog_filters_recency
        ON public.saved_catalog_filters (tenant_id, last_used_at DESC, saved_filter_id)
        """
    )
    op.execute(
        """
        CREATE INDEX saved_catalog_filters_by_name
        ON public.saved_catalog_filters (tenant_id, lower(name), saved_filter_id)
        """
    )
    op.execute("ALTER TABLE public.saved_catalog_filters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.saved_catalog_filters FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY saved_catalog_filters_tenant_isolation
        ON public.saved_catalog_filters
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.saved_catalog_filters FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.saved_catalog_filters TO ec_app"
    )


def downgrade() -> None:
    """Drop the saved-filter store and its isolation policy."""
    op.execute(
        "DROP POLICY IF EXISTS saved_catalog_filters_tenant_isolation "
        "ON public.saved_catalog_filters"
    )
    op.execute("DROP TABLE IF EXISTS public.saved_catalog_filters")
