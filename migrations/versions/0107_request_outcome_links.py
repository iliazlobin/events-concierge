"""Link a durable request to its explicitly selected lifecycle outcome.

Revision ID: 0107
Revises: 0106
Create Date: 2026-07-22

The parent request workflow and per-event child workflow use different stable identities. Existing
PostgreSQL rows therefore provide no reliable request-to-selected-child join: timestamps and
candidate order would confuse failed internal attempts with the user-visible outcome. This narrow,
immutable RLS link records only the selected lifecycle and lets its live state remain authoritative.
Legacy requests and requests that have not selected an outcome simply have no link.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: str | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

_ONLINE_UNIQUE_INDEXES = (
    (
        "uq_event_requests_tenant_request",
        "CREATE UNIQUE INDEX uq_event_requests_tenant_request "
        "ON public.event_requests USING btree (tenant_id, request_id)",
        """
        CREATE UNIQUE INDEX CONCURRENTLY uq_event_requests_tenant_request
        ON public.event_requests (tenant_id, request_id)
        """,
    ),
    (
        "uq_lifecycle_tenant_lifecycle",
        "CREATE UNIQUE INDEX uq_lifecycle_tenant_lifecycle "
        "ON public.lifecycle USING btree (tenant_id, lifecycle_id)",
        """
        CREATE UNIQUE INDEX CONCURRENTLY uq_lifecycle_tenant_lifecycle
        ON public.lifecycle (tenant_id, lifecycle_id)
        """,
    ),
)

_INDEX_STATE = sa.text(
    """
    SELECT
        index_state.indisvalid AS is_valid,
        index_state.indisready AS is_ready,
        index_state.indisunique AS is_unique,
        pg_get_indexdef(index_relation.oid) AS definition,
        EXISTS (
            SELECT 1
            FROM pg_constraint AS constraint_state
            WHERE constraint_state.conindid = index_relation.oid
        ) AS is_constraint_owned
    FROM pg_class AS index_relation
    JOIN pg_namespace AS index_namespace
      ON index_namespace.oid = index_relation.relnamespace
    JOIN pg_index AS index_state
      ON index_state.indexrelid = index_relation.oid
    WHERE index_namespace.nspname = 'public'
      AND index_relation.relname = :index_name
    """
)


def _ensure_online_unique_index(index_name: str, expected_definition: str, create_sql: str) -> None:
    """Create or recover one restart-safe concurrent index before constraint attachment.

    ``CREATE INDEX CONCURRENTLY`` commits outside Alembic's revision transaction. An interrupted
    first attempt can therefore leave a valid reusable index or an invalid catalog entry while the
    database still reports revision 0106. Reuse only the exact healthy index; remove every stale
    unattached variant before rebuilding so the ordinary ``alembic upgrade`` retry is sufficient.
    """
    row = (
        op.get_bind()
        .execute(_INDEX_STATE, {"index_name": index_name})
        .mappings()
        .one_or_none()
    )
    if row is not None and row["is_constraint_owned"]:
        raise RuntimeError(
            f"online migration index public.{index_name} is already constraint-owned while "
            "Alembic is below revision 0107"
        )
    reusable = row is not None and all(
        (
            row["is_valid"],
            row["is_ready"],
            row["is_unique"],
            row["definition"] == expected_definition,
        )
    )
    if reusable:
        return
    if row is not None:
        # Names are migration-owned constants, never caller-controlled identifiers.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{index_name}")
    op.execute(create_sql)


def upgrade() -> None:
    """Create one tenant-consistent selected-lifecycle link per durable request."""
    # These supporting keys touch pre-existing tenant tables. Build them without blocking writes;
    # attaching an already-valid unique index as a constraint is then a short metadata operation.
    # ``autocommit_block`` is required because PostgreSQL forbids CONCURRENTLY in a transaction.
    with op.get_context().autocommit_block():
        for index_name, expected_definition, create_sql in _ONLINE_UNIQUE_INDEXES:
            _ensure_online_unique_index(index_name, expected_definition, create_sql)
    op.execute(
        """
        ALTER TABLE public.event_requests
        ADD CONSTRAINT uq_event_requests_tenant_request
        UNIQUE USING INDEX uq_event_requests_tenant_request
        """
    )
    op.execute(
        """
        ALTER TABLE public.lifecycle
        ADD CONSTRAINT uq_lifecycle_tenant_lifecycle
        UNIQUE USING INDEX uq_lifecycle_tenant_lifecycle
        """
    )
    op.execute(
        """
        CREATE TABLE public.request_outcome_links (
            tenant_id   uuid NOT NULL,
            request_id  uuid NOT NULL,
            lifecycle_id uuid NOT NULL,
            linked_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, request_id),
            CONSTRAINT fk_request_outcome_links_request
                FOREIGN KEY (tenant_id, request_id)
                REFERENCES public.event_requests (tenant_id, request_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_request_outcome_links_lifecycle
                FOREIGN KEY (tenant_id, lifecycle_id)
                REFERENCES public.lifecycle (tenant_id, lifecycle_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_request_outcome_links_tenant_lifecycle
        ON public.request_outcome_links (tenant_id, lifecycle_id)
        """
    )
    op.execute("ALTER TABLE public.request_outcome_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.request_outcome_links FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY request_outcome_links_tenant_isolation
        ON public.request_outcome_links
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    # A selected link is append-only durable truth. A retried writer can re-read the existing row,
    # but ordinary runtime code cannot rewrite history to point a request at another lifecycle.
    op.execute("REVOKE ALL ON TABLE public.request_outcome_links FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.request_outcome_links FROM ec_app")
    op.execute("GRANT SELECT, INSERT ON TABLE public.request_outcome_links TO ec_app")


def downgrade() -> None:
    """Remove only the selected-outcome link and its supporting composite keys."""
    op.execute("DROP TABLE IF EXISTS public.request_outcome_links")
    op.execute(
        """
        ALTER TABLE public.lifecycle
        DROP CONSTRAINT IF EXISTS uq_lifecycle_tenant_lifecycle
        """
    )
    op.execute(
        """
        ALTER TABLE public.event_requests
        DROP CONSTRAINT IF EXISTS uq_event_requests_tenant_request
        """
    )
