"""Persist tenant-scoped declared and learned ranking affinities.

Revision ID: 0060
Revises: 0059
Create Date: 2026-07-18

The profile is deliberately only durable, tenant-isolated ranking input.  It stores declared
affinities and already-aggregated implicit affinities, without imposing product-level caps,
decay, or ownership policy.  Those semantics belong above persistence (FR-2.1, FR-4.3,
FR-1.2--FR-1.4, ADR-001).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create a narrow FORCE-RLS profile store for the ranking boundary."""
    op.execute(
        """
        CREATE FUNCTION public.fn_ranking_affinity_map_is_valid(p_value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT CASE
                WHEN p_value IS NULL OR jsonb_typeof(p_value) <> 'object' THEN false
                ELSE NOT EXISTS (
                    SELECT 1
                    FROM jsonb_each(p_value) AS entry(label, weight)
                    WHERE NULLIF(btrim(entry.label), '') IS NULL
                       OR jsonb_typeof(entry.weight) <> 'number'
                )
            END
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE public.tenant_ranking_profiles (
            tenant_id              uuid PRIMARY KEY
                                   REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            explicit_affinities    jsonb NOT NULL DEFAULT '{}'::jsonb,
            implicit_affinities    jsonb NOT NULL DEFAULT '{}'::jsonb,
            revision               bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT tenant_ranking_profiles_explicit_affinities_valid
                CHECK (public.fn_ranking_affinity_map_is_valid(explicit_affinities)),
            CONSTRAINT tenant_ranking_profiles_implicit_affinities_valid
                CHECK (public.fn_ranking_affinity_map_is_valid(implicit_affinities))
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_reject_tenant_ranking_profile_revision_regression()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.revision < OLD.revision THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'tenant ranking profile revision cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tenant_ranking_profiles_revision_monotonic
        BEFORE UPDATE ON public.tenant_ranking_profiles
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_reject_tenant_ranking_profile_revision_regression()
        """
    )
    op.execute("ALTER TABLE public.tenant_ranking_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_ranking_profiles FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_ranking_profiles_tenant_isolation
        ON public.tenant_ranking_profiles
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )

    # Application writes are tenant-scoped by FORCE RLS. Deletion remains an owner-only retention
    # operation; this migration deliberately adds no semantic profile-management policy.
    op.execute("REVOKE ALL ON TABLE public.tenant_ranking_profiles FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_ranking_profiles FROM ec_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.tenant_ranking_profiles TO ec_app")


def downgrade() -> None:
    """Remove only P8a profile storage and its local validation/monotonicity guards."""
    op.execute(
        "DROP TRIGGER IF EXISTS tenant_ranking_profiles_revision_monotonic "
        "ON public.tenant_ranking_profiles"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_reject_tenant_ranking_profile_revision_regression()"
    )
    op.execute("DROP TABLE IF EXISTS public.tenant_ranking_profiles")
    op.execute("DROP FUNCTION IF EXISTS public.fn_ranking_affinity_map_is_valid(jsonb)")
