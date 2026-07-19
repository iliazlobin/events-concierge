"""Persist immutable tenant feedback receipts for implicit ranking affinities.

Revision ID: 0092
Revises: 0091
Create Date: 2026-07-18

P17 records only an authenticated tenant's opaque signal identity, canonical-event reference,
closed signal kind, and already-derived affinity deltas.  The runtime role can append and read
under FORCE RLS, but cannot rewrite or erase a receipt; aggregation is derived exclusively from
those durable rows (FR-1.3/1.4, FR-4.3, NFR-8, ADR-001).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create an append-only RLS receipt stream for the feedback aggregation boundary."""
    op.execute(
        """
        CREATE FUNCTION public.fn_ranking_feedback_delta_map_is_valid(p_value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT CASE
                WHEN p_value IS NULL OR jsonb_typeof(p_value) <> 'object' THEN false
                WHEN (SELECT count(*) FROM jsonb_object_keys(p_value)) > 24 THEN false
                ELSE NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(p_value) AS entry(label, weight)
                    WHERE char_length(entry.label) NOT BETWEEN 1 AND 64
                       OR entry.label !~ '^[a-z0-9]+$'
                       OR jsonb_typeof(entry.weight) <> 'number'
                       OR CASE
                           WHEN jsonb_typeof(entry.weight) = 'number' THEN
                               (entry.weight #>> '{}')::numeric = 0
                               OR abs((entry.weight #>> '{}')::numeric) > 1
                           ELSE true
                       END
                )
            END
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE public.tenant_ranking_feedback_receipts (
            tenant_id          uuid NOT NULL
                               REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            signal_id          uuid NOT NULL,
            canonical_event_id uuid NOT NULL
                               REFERENCES public.canonical_events(canonical_event_id)
                               ON DELETE RESTRICT,
            signal_kind        text NOT NULL
                               CHECK (signal_kind IN ('scroll', 'dwell', 'click', 'dismiss')),
            feature_deltas     jsonb NOT NULL,
            recorded_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, signal_id),
            CONSTRAINT tenant_ranking_feedback_receipts_feature_deltas_valid
                CHECK (public.fn_ranking_feedback_delta_map_is_valid(feature_deltas))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_tenant_ranking_feedback_receipts_tenant_recorded
        ON public.tenant_ranking_feedback_receipts (tenant_id, recorded_at)
        """
    )
    op.execute("ALTER TABLE public.tenant_ranking_feedback_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_ranking_feedback_receipts FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_ranking_feedback_receipts_tenant_isolation
        ON public.tenant_ranking_feedback_receipts
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    # Receipts are immutable at runtime.  The adapter's INSERT-first replay protocol and the
    # composite identity make retries converge without granting UPDATE/DELETE authority.
    op.execute("REVOKE ALL ON TABLE public.tenant_ranking_feedback_receipts FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_ranking_feedback_receipts FROM ec_app")
    op.execute("GRANT SELECT, INSERT ON TABLE public.tenant_ranking_feedback_receipts TO ec_app")


def downgrade() -> None:
    """Remove only P17's feedback receipt stream and its derived-read index."""
    op.execute("DROP TABLE IF EXISTS public.tenant_ranking_feedback_receipts")
    op.execute("DROP FUNCTION IF EXISTS public.fn_ranking_feedback_delta_map_is_valid(jsonb)")
