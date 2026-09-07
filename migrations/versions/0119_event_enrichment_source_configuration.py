"""Persist public event enrichment and add audited source configuration updates.

Revision ID: 0119
Revises: 0118
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATE_SIGNATURE = (
    "(text,integer,text,text[],text,boolean,boolean,timestamp with time zone,"
    "integer,integer,integer,text)"
)


def upgrade() -> None:
    """Add bounded public role fields and one optimistic reviewed-write capability."""
    op.execute(
        """
        ALTER TABLE public.canonical_events
        ADD COLUMN organizer_name text,
        ADD COLUMN host_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN speaker_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN partner_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN attendance_count integer,
        ADD COLUMN registration_status text NOT NULL DEFAULT 'unknown',
        ADD CONSTRAINT ck_canonical_event_attendance_count
            CHECK (attendance_count IS NULL OR attendance_count BETWEEN 0 AND 10000000),
        ADD CONSTRAINT ck_canonical_event_registration_status
            CHECK (registration_status IN ('open', 'waitlist', 'sold_out', 'unknown')),
        ADD CONSTRAINT ck_canonical_event_public_role_counts
            CHECK (
                cardinality(host_names) <= 32
                AND cardinality(speaker_names) <= 32
                AND cardinality(partner_names) <= 32
            ),
        ADD CONSTRAINT ck_canonical_event_public_role_names
            CHECK (
                (organizer_name IS NULL OR char_length(organizer_name) BETWEEN 1 AND 160)
                AND array_position(host_names, NULL) IS NULL
                AND array_position(speaker_names, NULL) IS NULL
                AND array_position(partner_names, NULL) IS NULL
                AND array_position(host_names, '') IS NULL
                AND array_position(speaker_names, '') IS NULL
                AND array_position(partner_names, '') IS NULL
            )
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_source_configuration_audit (
            audit_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_key     text NOT NULL
                           REFERENCES public.catalog_sources(source_key) ON DELETE RESTRICT,
            prior_revision integer NOT NULL CHECK (prior_revision > 0),
            new_revision   integer NOT NULL CHECK (new_revision > prior_revision),
            requested_by   text NOT NULL
                           CHECK (
                               char_length(requested_by) BETWEEN 1 AND 256
                               AND requested_by !~ '[[:cntrl:]]'
                           ),
            changed_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
            before_config  jsonb NOT NULL CHECK (jsonb_typeof(before_config) = 'object'),
            after_config   jsonb NOT NULL CHECK (jsonb_typeof(after_config) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_source_configuration_audit_source
        ON public.catalog_source_configuration_audit
            (source_key, changed_at DESC, audit_id DESC)
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_update_ingestion_admin_source_configuration(
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
               OR p_mode NOT IN (
                    'public_jsonld', 'livewhale_json', 'sf_gov_json',
                    'datasf_our415', 'bibliocommons_rss',
                    'san_jose_legistar', 'sunnyvale_legistar',
                    'alameda_legistar', 'oakland_legistar',
                    'communico_json', 'tribe_events_json', 'localist_json',
                    'libcal_ics', 'civic_engage_rss', 'midpen_html',
                    'usfca_html', 'calperformances_json',
                    'berkeley_rep_html', 'ybca_html', 'oakland_html',
                    'luma_calendar_json', 'luma_discover_json'
               )
               OR p_enabled IS NULL
               OR p_handoff_only IS DISTINCT FROM true
               OR (
                    p_review_expires_at IS NOT NULL
                    AND p_review_expires_at <= v_now
               )
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
                mode = p_mode,
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
        """
    )
    op.execute(
        f"""
        REVOKE ALL ON FUNCTION
            public.fn_update_ingestion_admin_source_configuration{_UPDATE_SIGNATURE}
        FROM PUBLIC
        """
    )
    op.execute(
        f"""
        GRANT EXECUTE ON FUNCTION
            public.fn_update_ingestion_admin_source_configuration{_UPDATE_SIGNATURE}
        TO ec_app
        """
    )
    op.execute(
        "REVOKE ALL ON TABLE public.catalog_source_configuration_audit FROM PUBLIC, ec_app"
    )
    op.execute(
        "REVOKE ALL ON SEQUENCE public.catalog_source_configuration_audit_audit_id_seq "
        "FROM PUBLIC, ec_app"
    )


def downgrade() -> None:
    """Remove the reviewed-write capability and public enrichment fields."""
    op.execute(
        "DROP FUNCTION IF EXISTS "
        f"public.fn_update_ingestion_admin_source_configuration{_UPDATE_SIGNATURE}"
    )
    op.execute("DROP TABLE IF EXISTS public.catalog_source_configuration_audit")
    op.execute(
        """
        ALTER TABLE public.canonical_events
        DROP CONSTRAINT IF EXISTS ck_canonical_event_public_role_names,
        DROP CONSTRAINT IF EXISTS ck_canonical_event_public_role_counts,
        DROP CONSTRAINT IF EXISTS ck_canonical_event_registration_status,
        DROP CONSTRAINT IF EXISTS ck_canonical_event_attendance_count,
        DROP COLUMN IF EXISTS registration_status,
        DROP COLUMN IF EXISTS attendance_count,
        DROP COLUMN IF EXISTS partner_names,
        DROP COLUMN IF EXISTS speaker_names,
        DROP COLUMN IF EXISTS host_names,
        DROP COLUMN IF EXISTS organizer_name
        """
    )
