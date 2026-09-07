"""Preserve hash-covered event enrichment through paged catalog staging.

Revision ID: 0121
Revises: 0120
Create Date: 2026-07-28

The original paged-stage capability predates public organizer, role, attendance, and registration
fields. Its stored content hash covers those fields, but its normalized row did not, so promotion
could not reconstruct the hashed candidate. The v2 capability validates and stores that enrichment
while delegating the existing page/cursor and live-lease transition to the already-fenced v1
capability.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_V1 = (
    "public.fn_stage_paged_catalog_refresh_page"
    "(text,text,uuid,integer,integer,integer,jsonb,jsonb)"
)
_STAGE_V2 = (
    "public.fn_stage_paged_catalog_refresh_page_v2"
    "(text,text,uuid,integer,integer,integer,jsonb,jsonb)"
)
_READ_V1 = "public.fn_read_paged_catalog_refresh_stage(text,text,uuid,integer)"
_READ_V2 = "public.fn_read_paged_catalog_refresh_stage_v2(text,text,uuid,integer)"


def upgrade() -> None:
    """Add bounded enrichment columns and expose only the lossless stage/read capabilities."""
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_stage_candidates
        ADD COLUMN organizer_name text,
        ADD COLUMN host_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN speaker_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN partner_names text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN attendance_count integer,
        ADD COLUMN registration_status text NOT NULL DEFAULT 'unknown',
        ADD CONSTRAINT ck_catalog_refresh_stage_attendance_count
            CHECK (attendance_count IS NULL OR attendance_count BETWEEN 0 AND 10000000),
        ADD CONSTRAINT ck_catalog_refresh_stage_registration_status
            CHECK (registration_status IN ('open', 'waitlist', 'sold_out', 'unknown')),
        ADD CONSTRAINT ck_catalog_refresh_stage_public_role_counts
            CHECK (
                cardinality(host_names) <= 32
                AND cardinality(speaker_names) <= 32
                AND cardinality(partner_names) <= 32
            ),
        ADD CONSTRAINT ck_catalog_refresh_stage_public_role_names
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
    _create_stage_v2()
    _create_read_v2()
    for signature in (_STAGE_V1, _READ_V1):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    for signature in (_STAGE_V2, _READ_V2):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Restore the pre-enrichment paged-stage capabilities and schema."""
    for signature in (_STAGE_V2, _READ_V2):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for signature in (_STAGE_V1, _READ_V1):
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_stage_candidates
        DROP CONSTRAINT IF EXISTS ck_catalog_refresh_stage_public_role_names,
        DROP CONSTRAINT IF EXISTS ck_catalog_refresh_stage_public_role_counts,
        DROP CONSTRAINT IF EXISTS ck_catalog_refresh_stage_registration_status,
        DROP CONSTRAINT IF EXISTS ck_catalog_refresh_stage_attendance_count,
        DROP COLUMN IF EXISTS registration_status,
        DROP COLUMN IF EXISTS attendance_count,
        DROP COLUMN IF EXISTS partner_names,
        DROP COLUMN IF EXISTS speaker_names,
        DROP COLUMN IF EXISTS host_names,
        DROP COLUMN IF EXISTS organizer_name
        """
    )


def _create_stage_v2() -> None:
    """Wrap the fenced v1 page transition with lossless enrichment persistence."""
    op.execute(
        """
        CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page_v2(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer,
            p_page_number integer,
            p_raw_count integer,
            p_candidates jsonb,
            p_source_event_ids jsonb
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_candidate jsonb;
            v_role jsonb;
            v_base_candidates jsonb;
            v_outcome text;
            v_updated integer;
        BEGIN
            IF p_candidates IS NULL
               OR pg_catalog.jsonb_typeof(p_candidates) <> 'array'
            THEN
                RETURN 'invalid';
            END IF;

            FOR v_candidate IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_candidates)
            LOOP
                IF pg_catalog.jsonb_typeof(v_candidate) <> 'object' THEN
                    RETURN 'invalid';
                END IF;
                IF NOT (v_candidate ?& ARRAY[
                       'organizer_name', 'host_names', 'speaker_names', 'partner_names',
                       'attendance_count', 'registration_status'
                   ])
                THEN
                    RETURN 'invalid';
                END IF;
                IF pg_catalog.jsonb_typeof(v_candidate -> 'organizer_name')
                       NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'host_names') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'speaker_names') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'partner_names') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'attendance_count')
                       NOT IN ('number', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'registration_status') <> 'string'
                THEN
                    RETURN 'invalid';
                END IF;
                IF (
                       v_candidate ->> 'organizer_name' IS NOT NULL
                       AND (
                           char_length(v_candidate ->> 'organizer_name') NOT BETWEEN 1 AND 160
                           OR v_candidate ->> 'organizer_name' ~ '[[:cntrl:]]'
                       )
                   )
                   OR pg_catalog.jsonb_array_length(v_candidate -> 'host_names') > 32
                   OR pg_catalog.jsonb_array_length(v_candidate -> 'speaker_names') > 32
                   OR pg_catalog.jsonb_array_length(v_candidate -> 'partner_names') > 32
                   OR (
                       v_candidate ->> 'attendance_count' IS NOT NULL
                       AND (
                           v_candidate ->> 'attendance_count' !~ '^(0|[1-9][0-9]{0,7})$'
                           OR (v_candidate ->> 'attendance_count')::numeric > 10000000
                       )
                   )
                   OR v_candidate ->> 'registration_status'
                       NOT IN ('open', 'waitlist', 'sold_out', 'unknown')
                THEN
                    RETURN 'invalid';
                END IF;

                FOR v_role IN
                    SELECT value
                    FROM pg_catalog.jsonb_array_elements(
                        (v_candidate -> 'host_names')
                        || (v_candidate -> 'speaker_names')
                        || (v_candidate -> 'partner_names')
                    )
                LOOP
                    IF pg_catalog.jsonb_typeof(v_role) <> 'string'
                       OR char_length(v_role #>> '{}') NOT BETWEEN 1 AND 160
                       OR v_role #>> '{}' ~ '[[:cntrl:]]'
                    THEN
                        RETURN 'invalid';
                    END IF;
                END LOOP;
                IF EXISTS (
                    SELECT 1
                    FROM (
                        VALUES
                            ('host', v_candidate -> 'host_names'),
                            ('speaker', v_candidate -> 'speaker_names'),
                            ('partner', v_candidate -> 'partner_names')
                    ) AS role_group(role_kind, role_names)
                    CROSS JOIN LATERAL
                        pg_catalog.jsonb_array_elements(role_group.role_names) AS role(value)
                    GROUP BY role_group.role_kind
                    HAVING count(*) <> count(DISTINCT lower(role.value #>> '{}'))
                )
                THEN
                    RETURN 'invalid';
                END IF;
            END LOOP;

            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           candidate.value - ARRAY[
                               'organizer_name', 'host_names', 'speaker_names', 'partner_names',
                               'attendance_count', 'registration_status'
                           ]::text[]
                       ),
                       '[]'::jsonb
                   )
            INTO v_base_candidates
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS candidate(value);

            v_outcome := public.fn_stage_paged_catalog_refresh_page(
                p_source_key,
                p_run_key,
                p_lease_token,
                p_source_revision,
                p_page_number,
                p_raw_count,
                v_base_candidates,
                p_source_event_ids
            );
            IF v_outcome NOT IN ('more', 'terminal') THEN
                RETURN v_outcome;
            END IF;

            UPDATE public.catalog_refresh_stage_candidates AS candidate
            SET organizer_name = staged.value ->> 'organizer_name',
                host_names = ARRAY(
                    SELECT role.value #>> '{}'
                    FROM pg_catalog.jsonb_array_elements(
                        staged.value -> 'host_names'
                    ) AS role(value)
                ),
                speaker_names = ARRAY(
                    SELECT role.value #>> '{}'
                    FROM pg_catalog.jsonb_array_elements(
                        staged.value -> 'speaker_names'
                    ) AS role(value)
                ),
                partner_names = ARRAY(
                    SELECT role.value #>> '{}'
                    FROM pg_catalog.jsonb_array_elements(
                        staged.value -> 'partner_names'
                    ) AS role(value)
                ),
                attendance_count = (staged.value ->> 'attendance_count')::integer,
                registration_status = staged.value ->> 'registration_status'
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS staged(value)
            WHERE candidate.source_key = p_source_key
              AND candidate.run_key = p_run_key
              AND candidate.page_number = p_page_number
              AND candidate.source_event_id = staged.value ->> 'source_event_id';
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> pg_catalog.jsonb_array_length(p_candidates) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'paged catalog enrichment stage count did not match';
            END IF;
            RETURN v_outcome;
        END;
        $$
        """
    )


def _create_read_v2() -> None:
    """Project all normalized fields needed to reconstruct and verify a staged candidate."""
    op.execute(
        """
        CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage_v2(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer
        )
        RETURNS TABLE(
            source text,
            source_event_id text,
            title text,
            start_at timestamptz,
            end_at timestamptz,
            registration_url text,
            venue_name text,
            city text,
            description text,
            price_status text,
            organizer_name text,
            host_names text[],
            speaker_names text[],
            partner_names text[],
            attendance_count integer,
            registration_status text,
            content_hash text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
               OR p_source_revision IS NULL
               OR p_source_revision < 1
            THEN
                RETURN;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_refresh_runs AS refresh
                JOIN public.catalog_refresh_progress AS progress
                  ON progress.source_key = refresh.source_key
                 AND progress.run_key = refresh.run_key
                WHERE refresh.source_key = p_source_key
                  AND refresh.run_key = p_run_key
                  AND refresh.status = 'running'
                  AND refresh.lease_token = p_lease_token
                  AND refresh.lease_expires_at > pg_catalog.clock_timestamp()
                  AND progress.source_revision = p_source_revision
                  AND progress.terminal_page IS NOT NULL
            ) THEN
                RETURN;
            END IF;
            RETURN QUERY
            SELECT candidate.source,
                   candidate.source_event_id,
                   candidate.title,
                   candidate.start_at,
                   candidate.end_at,
                   candidate.registration_url,
                   candidate.venue_name,
                   candidate.city,
                   candidate.description,
                   candidate.price_status,
                   candidate.organizer_name,
                   candidate.host_names,
                   candidate.speaker_names,
                   candidate.partner_names,
                   candidate.attendance_count,
                   candidate.registration_status,
                   candidate.content_hash
            FROM public.catalog_refresh_stage_candidates AS candidate
            WHERE candidate.source_key = p_source_key
              AND candidate.run_key = p_run_key
            ORDER BY candidate.page_number, candidate.source_event_id;
        END;
        $$
        """
    )
