"""Keep reviewed source mode immutable in the compatible configuration capability.

Revision ID: 0181
Revises: 0180

The existing twelve-argument SQL interface remains callable. Mode is read under the same
row lock as the optimistic revision check, accepts a matching legacy value or NULL, and
is never reassigned from caller input. This admits already-reviewed meetup_city_jsonld
configuration updates without creating a new adapter-authority path.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0181"
down_revision: str | None = "0180"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.fn_update_ingestion_admin_source_configuration(
            p_source_key text,
            p_expected_revision integer,
            p_seed_url text,
            p_approved_origins text[],
            p_mode text,
            p_enabled boolean,
            p_handoff_only boolean,
            p_review_expires_at timestamptz,
            p_refresh_interval_minutes integer,
            p_min_interval_ms integer,
            p_page_limit integer,
            p_requested_by text
        )
        RETURNS TABLE (
            outcome text,
            source_revision integer,
            reviewed_at timestamptz,
            updated_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := clock_timestamp();
            v_source public.catalog_sources%ROWTYPE;
            v_updated public.catalog_sources%ROWTYPE;
            v_before jsonb;
            v_after jsonb;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_expected_revision IS NULL
               OR p_expected_revision < 1
               OR p_seed_url IS NULL
               OR char_length(p_seed_url) NOT BETWEEN 1 AND 2048
               OR p_seed_url ~ '[[:cntrl:]]'
               OR p_seed_url !~ '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/|[?]|$)'
               OR p_approved_origins IS NULL
               OR cardinality(p_approved_origins) NOT BETWEEN 1 AND 20
               OR EXISTS (
                    SELECT 1
                    FROM unnest(p_approved_origins) AS approved(origin)
                    WHERE approved.origin IS NULL
                       OR char_length(approved.origin) NOT BETWEEN 9 AND 512
                       OR approved.origin ~ '[[:cntrl:]]'
                       OR approved.origin
                          !~ '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$'
               )
               OR (
                    SELECT count(DISTINCT approved.origin)
                    FROM unnest(p_approved_origins) AS approved(origin)
               ) <> cardinality(p_approved_origins)
               OR NOT EXISTS (
                    SELECT 1
                    FROM unnest(p_approved_origins) AS approved(origin)
                    WHERE p_seed_url = approved.origin
                       OR p_seed_url LIKE approved.origin || '/%'
                       OR p_seed_url LIKE approved.origin || '?%'
               )
               OR p_enabled IS NULL
               OR p_handoff_only IS DISTINCT FROM true
               OR (
                    p_review_expires_at IS NOT NULL
                    AND p_review_expires_at <= v_now
               )
               OR p_refresh_interval_minutes IS NULL
               OR p_min_interval_ms IS NULL
               OR p_page_limit IS NULL
               OR p_refresh_interval_minutes NOT BETWEEN 5 AND 10080
               OR p_min_interval_ms NOT BETWEEN 250 AND 60000
               OR p_page_limit NOT BETWEEN 1 AND 500
               OR p_requested_by IS NULL
               OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
               OR p_requested_by ~ '[[:cntrl:]]'
            THEN
                RETURN QUERY SELECT 'invalid', NULL::integer, NULL::timestamptz,
                                    NULL::timestamptz;
                RETURN;
            END IF;

            SELECT source.*
            INTO v_source
            FROM public.catalog_sources AS source
            WHERE source.source_key = p_source_key
            FOR UPDATE;

            IF NOT FOUND THEN
                RETURN QUERY SELECT 'not_found', NULL::integer, NULL::timestamptz,
                                    NULL::timestamptz;
                RETURN;
            END IF;
            IF v_source.source_revision <> p_expected_revision THEN
                RETURN QUERY SELECT 'conflict', v_source.source_revision,
                                    v_source.reviewed_at, v_source.updated_at;
                RETURN;
            END IF;

            -- Mode is source identity, never an operator-editable configuration field.
            -- NULL supports newer callers omitting this legacy parameter; older callers must
            -- echo the stored mode. No duplicated allowlist can exclude a reviewed adapter.
            IF p_mode IS NOT NULL AND p_mode IS DISTINCT FROM v_source.mode THEN
                RETURN QUERY SELECT 'invalid', v_source.source_revision,
                                    v_source.reviewed_at, v_source.updated_at;
                RETURN;
            END IF;
            IF p_enabled AND v_source.retired_at IS NOT NULL THEN
                RETURN QUERY SELECT 'unavailable', v_source.source_revision,
                                    v_source.reviewed_at, v_source.updated_at;
                RETURN;
            END IF;

            v_before := jsonb_build_object(
                'seed_url', v_source.seed_url,
                'approved_origins', to_jsonb(v_source.approved_origins),
                'mode', v_source.mode,
                'enabled', v_source.enabled,
                'handoff_only', v_source.handoff_only,
                'reviewed_at', v_source.reviewed_at,
                'review_expires_at', v_source.review_expires_at,
                'refresh_interval_minutes', v_source.refresh_interval_minutes,
                'min_interval_ms', v_source.min_interval_ms,
                'page_limit', v_source.page_limit,
                'source_revision', v_source.source_revision
            );

            UPDATE public.catalog_sources AS source
            SET seed_url = p_seed_url,
                approved_origins = p_approved_origins,
                mode = v_source.mode,
                enabled = p_enabled,
                handoff_only = p_handoff_only,
                reviewed_at = v_now,
                review_expires_at = p_review_expires_at,
                refresh_interval_minutes = p_refresh_interval_minutes,
                min_interval_ms = p_min_interval_ms,
                page_limit = p_page_limit,
                source_revision = source.source_revision + 1,
                updated_at = v_now
            WHERE source.source_key = p_source_key
            RETURNING source.* INTO v_updated;

            v_after := jsonb_build_object(
                'seed_url', v_updated.seed_url,
                'approved_origins', to_jsonb(v_updated.approved_origins),
                'mode', v_updated.mode,
                'enabled', v_updated.enabled,
                'handoff_only', v_updated.handoff_only,
                'reviewed_at', v_updated.reviewed_at,
                'review_expires_at', v_updated.review_expires_at,
                'refresh_interval_minutes', v_updated.refresh_interval_minutes,
                'min_interval_ms', v_updated.min_interval_ms,
                'page_limit', v_updated.page_limit,
                'source_revision', v_updated.source_revision
            );

            INSERT INTO public.catalog_source_configuration_audit (
                source_key, prior_revision, new_revision, requested_by,
                changed_at, before_config, after_config
            )
            VALUES (
                p_source_key, v_source.source_revision, v_updated.source_revision,
                p_requested_by, v_now, v_before, v_after
            );

            RETURN QUERY SELECT 'updated', v_updated.source_revision,
                                v_updated.reviewed_at, v_updated.updated_at;
        END;
        $$
        """)


def downgrade() -> None:
    """Retain mode immutability when rolling back code; restoring the unsafe write is forbidden.

    The function signature and result type are unchanged, so old images remain compatible.
    """
