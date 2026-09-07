"""Persist exact event price ranges and search public people metadata.

Revision ID: 0123
Revises: 0122
Create Date: 2026-07-29

Exact amounts are optional even for paid events: they are public only when a source supplies one
complete, bounded range and a three-letter currency. The paged stage gets additive v3 capabilities
so old workers can finish against v2 during a rolling deployment. The filtered browse v2 capability
projects the new fields and applies people-name matching before keyset pagination.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0123"
down_revision: str | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_V3 = (
    "public.fn_stage_paged_catalog_refresh_page_v3"
    "(text,text,uuid,integer,integer,integer,jsonb,jsonb)"
)
_READ_V3 = "public.fn_read_paged_catalog_refresh_stage_v3(text,text,uuid,integer)"
_BROWSE_V2 = (
    "public.fn_browse_filtered_current_catalog_events_v2"
    "(text,timestamp with time zone,timestamp with time zone,text,text,text,"
    "timestamp with time zone,uuid,integer)"
)


def upgrade() -> None:
    """Add optional exact-money storage and lossless stage/read/browse capabilities."""
    for table, constraint in (
        ("canonical_events", "ck_canonical_event_price_range"),
        ("event_source_links", "ck_event_source_link_price_range"),
        ("catalog_event_observations", "ck_catalog_event_observation_price_range"),
        ("catalog_refresh_stage_candidates", "ck_catalog_refresh_stage_price_range"),
    ):
        op.execute(
            f"""
            ALTER TABLE public.{table}
            ADD COLUMN price_min_cents integer,
            ADD COLUMN price_max_cents integer,
            ADD COLUMN price_currency text,
            ADD CONSTRAINT {constraint}
                CHECK (
                    (
                        price_min_cents IS NULL
                        AND price_max_cents IS NULL
                        AND price_currency IS NULL
                    )
                    OR (
                        price_status = 'paid'
                        AND price_min_cents BETWEEN 1 AND 100000000
                        AND price_max_cents BETWEEN price_min_cents AND 100000000
                        AND price_currency ~ '^[A-Z]{{3}}$'
                    )
                )
            """
        )

    _create_stage_v3()
    _create_read_v3()
    _create_browse_v2()
    for signature in (_STAGE_V3, _READ_V3, _BROWSE_V2):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove only the v3/v2 additions and exact-money columns."""
    for signature in (_BROWSE_V2, _READ_V3, _STAGE_V3):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for table, constraint in (
        ("catalog_refresh_stage_candidates", "ck_catalog_refresh_stage_price_range"),
        ("catalog_event_observations", "ck_catalog_event_observation_price_range"),
        ("event_source_links", "ck_event_source_link_price_range"),
        ("canonical_events", "ck_canonical_event_price_range"),
    ):
        op.execute(
            f"""
            ALTER TABLE public.{table}
            DROP CONSTRAINT IF EXISTS {constraint},
            DROP COLUMN IF EXISTS price_currency,
            DROP COLUMN IF EXISTS price_max_cents,
            DROP COLUMN IF EXISTS price_min_cents
            """
        )


def _create_stage_v3() -> None:
    """Validate exact money, then delegate all lease/cursor/enrichment behavior to v2."""
    op.execute(
        """
        CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page_v3(
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
                       'price_min_cents', 'price_max_cents', 'price_currency'
                   ])
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'price_min_cents')
                       NOT IN ('number', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'price_max_cents')
                       NOT IN ('number', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'price_currency')
                       NOT IN ('string', 'null')
                THEN
                    RETURN 'invalid';
                END IF;

                IF v_candidate ->> 'price_min_cents' IS NULL
                   AND v_candidate ->> 'price_max_cents' IS NULL
                   AND v_candidate ->> 'price_currency' IS NULL
                THEN
                    NULL;
                ELSIF v_candidate ->> 'price_status' <> 'paid'
                   OR v_candidate ->> 'price_min_cents' IS NULL
                   OR v_candidate ->> 'price_max_cents' IS NULL
                   OR v_candidate ->> 'price_currency' IS NULL
                   OR v_candidate ->> 'price_min_cents' !~ '^[1-9][0-9]{0,8}$'
                   OR v_candidate ->> 'price_max_cents' !~ '^[1-9][0-9]{0,8}$'
                   OR (v_candidate ->> 'price_min_cents')::numeric > 100000000
                   OR (v_candidate ->> 'price_max_cents')::numeric > 100000000
                   OR (v_candidate ->> 'price_max_cents')::numeric
                      < (v_candidate ->> 'price_min_cents')::numeric
                   OR v_candidate ->> 'price_currency' !~ '^[A-Z]{3}$'
                THEN
                    RETURN 'invalid';
                END IF;
            END LOOP;

            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           candidate.value - ARRAY[
                               'price_min_cents', 'price_max_cents', 'price_currency'
                           ]::text[]
                       ),
                       '[]'::jsonb
                   )
            INTO v_base_candidates
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS candidate(value);

            v_outcome := public.fn_stage_paged_catalog_refresh_page_v2(
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
            SET price_min_cents = (staged.value ->> 'price_min_cents')::integer,
                price_max_cents = (staged.value ->> 'price_max_cents')::integer,
                price_currency = staged.value ->> 'price_currency'
            FROM pg_catalog.jsonb_array_elements(p_candidates) AS staged(value)
            WHERE candidate.source_key = p_source_key
              AND candidate.run_key = p_run_key
              AND candidate.page_number = p_page_number
              AND candidate.source_event_id = staged.value ->> 'source_event_id';
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> pg_catalog.jsonb_array_length(p_candidates) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'paged catalog price stage count did not match';
            END IF;
            RETURN v_outcome;
        END;
        $$
        """
    )


def _create_read_v3() -> None:
    """Project every hash-covered field needed for terminal-stage promotion."""
    op.execute(
        """
        CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage_v3(
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
                   candidate.price_min_cents,
                   candidate.price_max_cents,
                   candidate.price_currency,
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


def _create_browse_v2() -> None:
    """Search public roles before paging and project current canonical enrichment and pricing."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v2(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_city text,
            p_price text,
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
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
               OR ((p_after_start IS NULL) <> (p_after_id IS NULL))
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR p_window_end - p_window_start > interval '370 days'
                   )
               )
               OR length(coalesce(p_query, '')) > 160
               OR length(coalesce(p_city, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR p_city ~ '[\x00-\x1f\x7f]'
               OR (
                   p_price IS NOT NULL
                   AND p_price NOT IN ('free', 'paid', 'unknown')
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'filtered catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH source_facts AS MATERIALIZED (
                SELECT source.*,
                       CASE
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?luma\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'luma'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?meetup\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'meetup'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?eventbrite\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'eventbrite'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?ticketmaster\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'ticketmaster'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?partiful\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'partiful'
                           ELSE lower(
                               substring(source.seed_url from '^https://([^/:]+)')
                           )
                       END AS provider_key
                FROM public.catalog_sources AS source
                WHERE source.enabled
                  AND source.handoff_only
                  AND source.reviewed_at IS NOT NULL
                  AND (
                      source.review_expires_at IS NULL
                      OR source.review_expires_at > statement_timestamp()
                  )
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                      source.source_key, source.publisher, source.seed_url
                  )
                  AND (p_source_key IS NULL OR source.source_key = p_source_key)
            ), latest_success AS MATERIALIZED (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.run_key
                FROM public.catalog_refresh_runs AS refresh
                JOIN source_facts AS source
                  ON source.source_key = refresh.source_key
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            ), current_observations AS MATERIALIZED (
                SELECT observation.*,
                       source.display_name AS source_label,
                       source.publisher,
                       source.provider_key,
                       source.seed_url
                FROM public.catalog_event_observations AS observation
                JOIN latest_success AS success
                  ON success.source_key = observation.source_key
                 AND success.run_key = observation.last_run_key
                JOIN source_facts AS source
                  ON source.source_key = observation.source_key
            ), eligible_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.start_at
                FROM public.canonical_events AS event
                JOIN current_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                WHERE event.start_at >= statement_timestamp()
                  AND event.event_status <> 'cancelled'
                  AND (
                      p_window_start IS NULL
                      OR (
                          event.start_at >= p_window_start
                          AND event.start_at < p_window_end
                      )
                  )
                  AND (
                      nullif(btrim(p_query), '') IS NULL
                      OR position(
                          lower(btrim(p_query))
                          IN lower(
                              concat_ws(
                                  ' ',
                                  event.title,
                                  event.description,
                                  event.venue_name,
                                  event.city_norm,
                                  event.organizer_name,
                                  array_to_string(event.host_names, ' '),
                                  array_to_string(event.speaker_names, ' '),
                                  array_to_string(event.partner_names, ' '),
                                  observation.source_label,
                                  observation.publisher,
                                  observation.provider_key
                              )
                          )
                      ) > 0
                  )
                  AND (
                      nullif(btrim(p_city), '') IS NULL
                      OR event.city_norm = regexp_replace(
                          lower(btrim(p_city)), '[^a-z0-9]+', '', 'g'
                      )
                  )
                  AND (p_price IS NULL OR event.price_status = p_price)
                  AND (
                      p_after_start IS NULL
                      OR event.start_at > p_after_start
                      OR (
                          event.start_at = p_after_start
                          AND event.canonical_event_id > p_after_id
                      )
                  )
                GROUP BY event.canonical_event_id, event.start_at
            ), page AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       eligible.start_at
                FROM eligible_events AS eligible
                ORDER BY eligible.start_at, eligible.canonical_event_id
                LIMIT p_limit
            )
            SELECT event.canonical_event_id,
                   event.title,
                   event.start_at,
                   event.end_at,
                   event.venue_name,
                   event.lat,
                   event.lon,
                   event.city_norm,
                   event.description,
                   event.price_status,
                   event.price_min_cents,
                   event.price_max_cents,
                   event.price_currency,
                   event.event_status,
                   event.normalizer_version,
                   event.merge_version,
                   event.organizer_name,
                   event.host_names,
                   event.speaker_names,
                   event.partner_names,
                   event.attendance_count,
                   event.registration_status,
                   observation.source_key,
                   observation.source_label,
                   observation.publisher,
                   observation.provider_key,
                   observation.seed_url,
                   observation.source,
                   observation.source_event_id,
                   observation.registration_url,
                   observation.last_seen_at,
                   observation.last_run_key
            FROM page
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = page.canonical_event_id
            JOIN current_observations AS observation
              ON observation.canonical_event_id = event.canonical_event_id
            ORDER BY page.start_at,
                     page.canonical_event_id,
                     observation.source_key,
                     observation.source,
                     observation.source_event_id;
        END;
        $$
        """
    )
