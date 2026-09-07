"""Persist only verified direct public entity profile URLs.

Revision ID: 0128
Revises: 0127
Create Date: 2026-07-30

Profiles are bounded canonical enrichment. They are never inferred lookup/search URLs, and every
profile name must already be present in the event's public organizer/host/speaker/partner fields.
The additive v4 capabilities preserve rolling workers on v3 while carrying the new JSONB field
losslessly through paged staging and chronological browse.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0128"
down_revision: str | None = "0127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALIDATOR = (
    "public.fn_event_entity_profiles_valid(jsonb,text,text[],text[],text[])"
)
_STAGE_V4 = (
    "public.fn_stage_paged_catalog_refresh_page_v4"
    "(text,text,uuid,integer,integer,integer,jsonb,jsonb)"
)
_READ_V4 = "public.fn_read_paged_catalog_refresh_stage_v4(text,text,uuid,integer)"
_BROWSE_V4 = (
    "public.fn_browse_filtered_current_catalog_events_v4"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "timestamp with time zone,uuid,integer)"
)


def upgrade() -> None:
    """Add checked profile storage and lossless v4 stage/read/browse capabilities."""
    _create_validator()
    op.execute(
        """
        ALTER TABLE public.canonical_events
        ADD COLUMN entity_profiles jsonb NOT NULL DEFAULT '[]'::jsonb,
        ADD CONSTRAINT ck_canonical_event_entity_profiles
            CHECK (
                public.fn_event_entity_profiles_valid(
                    entity_profiles,
                    organizer_name,
                    host_names,
                    speaker_names,
                    partner_names
                )
            )
        """
    )
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_stage_candidates
        ADD COLUMN entity_profiles jsonb NOT NULL DEFAULT '[]'::jsonb,
        ADD CONSTRAINT ck_catalog_refresh_stage_entity_profiles
            CHECK (
                public.fn_event_entity_profiles_valid(
                    entity_profiles,
                    organizer_name,
                    host_names,
                    speaker_names,
                    partner_names
                )
            )
        """
    )
    _create_stage_v4()
    _create_read_v4()
    _create_browse_v4()
    for signature in (_VALIDATOR, _STAGE_V4, _READ_V4, _BROWSE_V4):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove only 0128 capabilities and columns; keep every v3 rolling capability."""
    for signature in (_BROWSE_V4, _READ_V4, _STAGE_V4):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_stage_candidates
        DROP CONSTRAINT IF EXISTS ck_catalog_refresh_stage_entity_profiles,
        DROP COLUMN IF EXISTS entity_profiles
        """
    )
    op.execute(
        """
        ALTER TABLE public.canonical_events
        DROP CONSTRAINT IF EXISTS ck_canonical_event_entity_profiles,
        DROP COLUMN IF EXISTS entity_profiles
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_VALIDATOR} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {_VALIDATOR}")


def _create_validator() -> None:
    """Use one immutable shape/attachment check for canonical and staged rows."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_event_entity_profiles_valid(
            p_profiles jsonb,
            p_organizer_name text,
            p_host_names text[],
            p_speaker_names text[],
            p_partner_names text[]
        )
        RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_profile jsonb;
            v_name text;
            v_role text;
            v_kind text;
            v_url text;
            v_identity text;
            v_seen text[] := ARRAY[]::text[];
            v_is_linkedin boolean;
        BEGIN
            IF p_profiles IS NULL
               OR pg_catalog.jsonb_typeof(p_profiles) <> 'array'
               OR pg_catalog.jsonb_array_length(p_profiles) > 64
            THEN
                RETURN false;
            END IF;

            FOR v_profile IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_profiles)
            LOOP
                IF pg_catalog.jsonb_typeof(v_profile) <> 'object'
                   OR (
                       SELECT count(*)
                       FROM pg_catalog.jsonb_object_keys(v_profile)
                   ) <> 4
                   OR NOT (v_profile ?& ARRAY['name', 'role', 'kind', 'profile_url'])
                   OR pg_catalog.jsonb_typeof(v_profile -> 'name') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_profile -> 'role') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_profile -> 'kind') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_profile -> 'profile_url') <> 'string'
                THEN
                    RETURN false;
                END IF;
                v_name := v_profile ->> 'name';
                v_role := v_profile ->> 'role';
                v_kind := v_profile ->> 'kind';
                v_url := v_profile ->> 'profile_url';

                IF char_length(v_name) NOT BETWEEN 1 AND 160
                   OR v_name ~ '[[:cntrl:]]'
                   OR v_role NOT IN ('host', 'organizer', 'speaker', 'partner')
                   OR v_kind NOT IN ('person', 'organization')
                   OR char_length(v_url) NOT BETWEEN 1 AND 2048
                   OR v_url ~ '[[:cntrl:][:space:]]'
                   OR v_url !~ '^https://[^/?#@[:space:]]+([/?#].*)?$'
                THEN
                    RETURN false;
                END IF;

                v_is_linkedin := v_url ~* (
                    '^https://(www[.])?linkedin[.]com'
                    '(:[0-9]{1,5})?(/|[?]|[#]|$)'
                );
                IF v_kind = 'person' AND v_url !~* (
                    '^https://(www[.])?linkedin[.]com'
                    '(:[0-9]{1,5})?/in/[a-z0-9][a-z0-9_%.-]*/?$'
                ) THEN
                    RETURN false;
                ELSIF v_kind = 'organization'
                      AND v_is_linkedin
                      AND v_url !~* (
                          '^https://(www[.])?linkedin[.]com'
                          '(:[0-9]{1,5})?/company/[a-z0-9][a-z0-9_%.-]*/?$'
                      )
                THEN
                    RETURN false;
                END IF;

                IF (
                    v_role = 'organizer'
                    AND (
                        p_organizer_name IS NULL
                        OR lower(v_name) <> lower(p_organizer_name)
                    )
                ) OR (
                    v_role = 'host'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_catalog.unnest(
                            COALESCE(p_host_names, ARRAY[]::text[])
                        ) AS displayed(name)
                        WHERE lower(displayed.name) = lower(v_name)
                    )
                ) OR (
                    v_role = 'speaker'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_catalog.unnest(
                            COALESCE(p_speaker_names, ARRAY[]::text[])
                        ) AS displayed(name)
                        WHERE lower(displayed.name) = lower(v_name)
                    )
                ) OR (
                    v_role = 'partner'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_catalog.unnest(
                            COALESCE(p_partner_names, ARRAY[]::text[])
                        ) AS displayed(name)
                        WHERE lower(displayed.name) = lower(v_name)
                    )
                ) THEN
                    RETURN false;
                END IF;

                v_identity := lower(v_role) || pg_catalog.chr(31) || lower(v_name);
                IF v_identity = ANY(v_seen) THEN
                    RETURN false;
                END IF;
                v_seen := pg_catalog.array_append(v_seen, v_identity);
            END LOOP;
            RETURN true;
        END;
        $$
        """
    )


def _create_stage_v4() -> None:
    """Validate profiles, delegate the lease/cursor transition, then persist JSONB atomically."""
    op.execute(
        """
        CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page_v4(
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
                IF pg_catalog.jsonb_typeof(v_candidate) <> 'object'
                   OR NOT (v_candidate ?& ARRAY[
                       'entity_profiles', 'organizer_name', 'host_names',
                       'speaker_names', 'partner_names'
                   ])
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'entity_profiles') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'organizer_name')
                       NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'host_names') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'speaker_names') <> 'array'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'partner_names') <> 'array'
                   OR NOT public.fn_event_entity_profiles_valid(
                       v_candidate -> 'entity_profiles',
                       v_candidate ->> 'organizer_name',
                       ARRAY(
                           SELECT value
                           FROM pg_catalog.jsonb_array_elements_text(
                               v_candidate -> 'host_names'
                           ) AS host(value)
                       ),
                       ARRAY(
                           SELECT value
                           FROM pg_catalog.jsonb_array_elements_text(
                               v_candidate -> 'speaker_names'
                           ) AS speaker(value)
                       ),
                       ARRAY(
                           SELECT value
                           FROM pg_catalog.jsonb_array_elements_text(
                               v_candidate -> 'partner_names'
                           ) AS partner(value)
                       )
                   )
                THEN
                    RETURN 'invalid';
                END IF;
            END LOOP;

            SELECT COALESCE(
                       pg_catalog.jsonb_agg(candidate.value - 'entity_profiles'),
                       '[]'::jsonb
                   )
            INTO v_base_candidates
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS candidate(value);

            v_outcome := public.fn_stage_paged_catalog_refresh_page_v3(
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
            SET entity_profiles = staged.value -> 'entity_profiles'
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS staged(value)
            WHERE candidate.source_key = p_source_key
              AND candidate.run_key = p_run_key
              AND candidate.page_number = p_page_number
              AND candidate.source_event_id = staged.value ->> 'source_event_id';
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> pg_catalog.jsonb_array_length(p_candidates) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'paged catalog entity profile stage count did not match';
            END IF;
            RETURN v_outcome;
        END;
        $$
        """
    )


def _create_read_v4() -> None:
    """Project profile JSON only for rows already admitted by the v3 lease/stage fence."""
    op.execute(
        """
        CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage_v4(
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
            price_min_cents integer,
            price_max_cents integer,
            price_currency text,
            organizer_name text,
            host_names text[],
            speaker_names text[],
            partner_names text[],
            attendance_count integer,
            registration_status text,
            entity_profiles jsonb,
            content_hash text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT staged.source,
                   staged.source_event_id,
                   staged.title,
                   staged.start_at,
                   staged.end_at,
                   staged.registration_url,
                   staged.venue_name,
                   staged.city,
                   staged.description,
                   staged.price_status,
                   staged.price_min_cents,
                   staged.price_max_cents,
                   staged.price_currency,
                   staged.organizer_name,
                   staged.host_names,
                   staged.speaker_names,
                   staged.partner_names,
                   staged.attendance_count,
                   staged.registration_status,
                   candidate.entity_profiles,
                   staged.content_hash
            FROM public.fn_read_paged_catalog_refresh_stage_v3(
                p_source_key, p_run_key, p_lease_token, p_source_revision
            ) AS staged
            JOIN public.catalog_refresh_stage_candidates AS candidate
              ON candidate.source_key = p_source_key
             AND candidate.run_key = p_run_key
             AND candidate.source_event_id = staged.source_event_id
            ORDER BY candidate.page_number, candidate.source_event_id
        $$
        """
    )


def _create_browse_v4() -> None:
    """Preserve v3 filtering/keyset semantics and add checked canonical profile JSON."""
    op.execute(
        """
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v4(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_after_start timestamptz,
            p_after_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid,
            title text,
            start_at timestamptz,
            end_at timestamptz,
            venue_name text,
            lat double precision,
            lon double precision,
            city_norm text,
            description text,
            price_status text,
            price_min_cents integer,
            price_max_cents integer,
            price_currency text,
            event_status text,
            normalizer_version integer,
            merge_version integer,
            organizer_name text,
            host_names text[],
            speaker_names text[],
            partner_names text[],
            attendance_count integer,
            registration_status text,
            entity_profiles jsonb,
            source_key text,
            source_label text,
            publisher text,
            provider text,
            seed_url text,
            observation_source text,
            source_event_id text,
            registration_url text,
            last_seen_at timestamptz,
            refresh_run_key text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT browse.canonical_event_id,
                   browse.title,
                   browse.start_at,
                   browse.end_at,
                   browse.venue_name,
                   browse.lat,
                   browse.lon,
                   browse.city_norm,
                   browse.description,
                   browse.price_status,
                   browse.price_min_cents,
                   browse.price_max_cents,
                   browse.price_currency,
                   browse.event_status,
                   browse.normalizer_version,
                   browse.merge_version,
                   browse.organizer_name,
                   browse.host_names,
                   browse.speaker_names,
                   browse.partner_names,
                   browse.attendance_count,
                   browse.registration_status,
                   event.entity_profiles,
                   browse.source_key,
                   browse.source_label,
                   browse.publisher,
                   browse.provider,
                   browse.seed_url,
                   browse.observation_source,
                   browse.source_event_id,
                   browse.registration_url,
                   browse.last_seen_at,
                   browse.refresh_run_key
            FROM public.fn_browse_filtered_current_catalog_events_v3(
                p_source_key,
                p_window_start,
                p_window_end,
                p_query,
                p_cities,
                p_location_scopes,
                p_price,
                p_price_max_cents,
                p_after_start,
                p_after_id,
                p_limit
            ) AS browse
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = browse.canonical_event_id
            ORDER BY browse.start_at,
                     browse.canonical_event_id,
                     browse.source_key,
                     browse.observation_source,
                     browse.source_event_id
        $$
        """
    )
