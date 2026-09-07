"""Make paged catalog topic filtering independent of temporary relation OIDs.

Revision ID: 0138
Revises: 0137
Create Date: 2026-07-31

The v7 browse function introduced in 0136 dropped and recreated two temporary tables inside its
scan loop.  PostgreSQL can retain the relation OIDs in the PL/pgSQL statement plan, so a broad
topic query that advances to a second 101-event scan can fail with ``could not open relation with
OID``.  Stream each bounded v6 scan through a record loop instead.  The externally visible keyset
and AND-topic contracts remain unchanged, while no temporary relation survives between scans.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0138"
down_revision: str | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE_V7 = (
    "public.fn_browse_filtered_current_catalog_events_v7"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "text[],timestamp with time zone,uuid,integer)"
)


def _replace_browse_v7_without_temp_relations() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.fn_browse_filtered_current_catalog_events_v7(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_topics text[],
            p_after_start timestamptz,
            p_after_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid, title text, start_at timestamptz, end_at timestamptz,
            venue_name text, lat double precision, lon double precision, city_norm text,
            description text, price_status text, price_min_cents integer,
            price_max_cents integer, price_currency text, event_status text,
            normalizer_version integer, merge_version integer, organizer_name text,
            host_names text[], speaker_names text[], partner_names text[],
            attendance_count integer, registration_status text, entity_profiles jsonb,
            source_key text, source_label text, publisher text, provider text, seed_url text,
            observation_source text, source_event_id text, registration_url text,
            last_seen_at timestamptz, refresh_run_key text, topics text[],
            extraction_evidence jsonb
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_after_start timestamptz := p_after_start;
            v_after_id uuid := p_after_id;
            v_batch_events integer;
            v_returned integer := 0;
            v_current_event_id uuid;
            v_current_selected boolean;
            v_row record;
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR cardinality(coalesce(p_topics, ARRAY[]::text[])) > 12
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_topics, ARRAY[]::text[])) AS topic(value)
                   WHERE topic.value IS NULL OR topic.value NOT IN (
                       'ai', 'arts', 'community', 'education', 'family', 'food-drink',
                       'founders', 'gaming', 'government', 'music', 'networking', 'outdoors', 'sports',
                       'technology', 'volleyball', 'wellness', 'workshop'
                   )
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog topic query is invalid';
            END IF;

            LOOP
                v_batch_events := 0;
                v_current_event_id := NULL;
                v_current_selected := false;

                FOR v_row IN
                    SELECT scan.canonical_event_id, scan.title, scan.start_at, scan.end_at,
                           scan.venue_name, scan.lat, scan.lon, scan.city_norm, scan.description,
                           scan.price_status, scan.price_min_cents, scan.price_max_cents,
                           scan.price_currency, scan.event_status, scan.normalizer_version,
                           scan.merge_version, scan.organizer_name, scan.host_names,
                           scan.speaker_names, scan.partner_names, scan.attendance_count,
                           scan.registration_status, scan.entity_profiles, scan.source_key,
                           scan.source_label, scan.publisher, scan.provider, scan.seed_url,
                           scan.observation_source, scan.source_event_id, scan.registration_url,
                           scan.last_seen_at, scan.refresh_run_key,
                           event.topics AS event_topics,
                           event.extraction_evidence AS event_extraction_evidence
                    FROM public.fn_browse_filtered_current_catalog_events_v6(
                        p_source_key, p_window_start, p_window_end, p_query, p_cities,
                        p_location_scopes, p_price, p_price_max_cents,
                        v_after_start, v_after_id, 101
                    ) AS scan
                    JOIN public.canonical_events AS event
                      ON event.canonical_event_id = scan.canonical_event_id
                    ORDER BY scan.start_at, scan.canonical_event_id, scan.source_key,
                             scan.observation_source, scan.source_event_id
                LOOP
                    IF v_current_event_id IS DISTINCT FROM v_row.canonical_event_id THEN
                        v_current_event_id := v_row.canonical_event_id;
                        v_batch_events := v_batch_events + 1;
                        v_after_start := v_row.start_at;
                        v_after_id := v_row.canonical_event_id;
                        v_current_selected := v_returned < p_limit
                            AND v_row.event_topics @> coalesce(p_topics, ARRAY[]::text[]);
                        IF v_current_selected THEN
                            v_returned := v_returned + 1;
                        END IF;
                    END IF;

                    IF v_current_selected THEN
                        canonical_event_id := v_row.canonical_event_id;
                        title := v_row.title;
                        start_at := v_row.start_at;
                        end_at := v_row.end_at;
                        venue_name := v_row.venue_name;
                        lat := v_row.lat;
                        lon := v_row.lon;
                        city_norm := v_row.city_norm;
                        description := v_row.description;
                        price_status := v_row.price_status;
                        price_min_cents := v_row.price_min_cents;
                        price_max_cents := v_row.price_max_cents;
                        price_currency := v_row.price_currency;
                        event_status := v_row.event_status;
                        normalizer_version := v_row.normalizer_version;
                        merge_version := v_row.merge_version;
                        organizer_name := v_row.organizer_name;
                        host_names := v_row.host_names;
                        speaker_names := v_row.speaker_names;
                        partner_names := v_row.partner_names;
                        attendance_count := v_row.attendance_count;
                        registration_status := v_row.registration_status;
                        entity_profiles := v_row.entity_profiles;
                        source_key := v_row.source_key;
                        source_label := v_row.source_label;
                        publisher := v_row.publisher;
                        provider := v_row.provider;
                        seed_url := v_row.seed_url;
                        observation_source := v_row.observation_source;
                        source_event_id := v_row.source_event_id;
                        registration_url := v_row.registration_url;
                        last_seen_at := v_row.last_seen_at;
                        refresh_run_key := v_row.refresh_run_key;
                        topics := v_row.event_topics;
                        extraction_evidence := v_row.event_extraction_evidence;
                        RETURN NEXT;
                    END IF;
                END LOOP;

                EXIT WHEN v_batch_events = 0
                          OR v_returned >= p_limit
                          OR v_batch_events < 101;
            END LOOP;
        END;
        $$
        """
    )


def _restore_browse_v7_from_0136() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.fn_browse_filtered_current_catalog_events_v7(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_topics text[],
            p_after_start timestamptz,
            p_after_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid, title text, start_at timestamptz, end_at timestamptz,
            venue_name text, lat double precision, lon double precision, city_norm text,
            description text, price_status text, price_min_cents integer,
            price_max_cents integer, price_currency text, event_status text,
            normalizer_version integer, merge_version integer, organizer_name text,
            host_names text[], speaker_names text[], partner_names text[],
            attendance_count integer, registration_status text, entity_profiles jsonb,
            source_key text, source_label text, publisher text, provider text, seed_url text,
            observation_source text, source_event_id text, registration_url text,
            last_seen_at timestamptz, refresh_run_key text, topics text[],
            extraction_evidence jsonb
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_after_start timestamptz := p_after_start;
            v_after_id uuid := p_after_id;
            v_batch_events integer;
            v_selected_events integer;
            v_returned integer := 0;
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR cardinality(coalesce(p_topics, ARRAY[]::text[])) > 12
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_topics, ARRAY[]::text[])) AS topic(value)
                   WHERE topic.value IS NULL OR topic.value NOT IN (
                       'ai', 'arts', 'community', 'education', 'family', 'food-drink',
                       'founders', 'gaming', 'government', 'music', 'networking', 'outdoors', 'sports',
                       'technology', 'volleyball', 'wellness', 'workshop'
                   )
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog topic query is invalid';
            END IF;

            LOOP
                DROP TABLE IF EXISTS pg_temp.catalog_topic_scan;
                CREATE TEMP TABLE catalog_topic_scan ON COMMIT DROP AS
                SELECT *
                FROM public.fn_browse_filtered_current_catalog_events_v6(
                    p_source_key, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents,
                    v_after_start, v_after_id, 101
                );

                SELECT count(DISTINCT scan.canonical_event_id)
                INTO v_batch_events
                FROM catalog_topic_scan AS scan;
                EXIT WHEN v_batch_events = 0;

                DROP TABLE IF EXISTS pg_temp.catalog_topic_selected;
                CREATE TEMP TABLE catalog_topic_selected ON COMMIT DROP AS
                SELECT DISTINCT scan.canonical_event_id, scan.start_at
                FROM catalog_topic_scan AS scan
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = scan.canonical_event_id
                WHERE event.topics @> coalesce(p_topics, ARRAY[]::text[])
                ORDER BY scan.start_at, scan.canonical_event_id
                LIMIT (p_limit - v_returned);

                SELECT count(*) INTO v_selected_events FROM catalog_topic_selected;
                RETURN QUERY
                SELECT scan.canonical_event_id, scan.title, scan.start_at, scan.end_at,
                       scan.venue_name, scan.lat, scan.lon, scan.city_norm, scan.description,
                       scan.price_status, scan.price_min_cents, scan.price_max_cents,
                       scan.price_currency, scan.event_status, scan.normalizer_version,
                       scan.merge_version, scan.organizer_name, scan.host_names,
                       scan.speaker_names, scan.partner_names, scan.attendance_count,
                       scan.registration_status, scan.entity_profiles, scan.source_key,
                       scan.source_label, scan.publisher, scan.provider, scan.seed_url,
                       scan.observation_source, scan.source_event_id, scan.registration_url,
                       scan.last_seen_at, scan.refresh_run_key, event.topics,
                       event.extraction_evidence
                FROM catalog_topic_scan AS scan
                JOIN catalog_topic_selected AS selected
                  ON selected.canonical_event_id = scan.canonical_event_id
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = scan.canonical_event_id
                ORDER BY scan.start_at, scan.canonical_event_id, scan.source_key,
                         scan.observation_source, scan.source_event_id;

                v_returned := v_returned + v_selected_events;
                EXIT WHEN v_returned >= p_limit OR v_batch_events < 101;
                SELECT scan.start_at, scan.canonical_event_id
                INTO v_after_start, v_after_id
                FROM catalog_topic_scan AS scan
                GROUP BY scan.start_at, scan.canonical_event_id
                ORDER BY scan.start_at DESC, scan.canonical_event_id DESC
                LIMIT 1;
            END LOOP;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Replace OID-sensitive temporary scan tables with record streaming."""
    _replace_browse_v7_without_temp_relations()
    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_V7} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BROWSE_V7} TO ec_app")


def downgrade() -> None:
    """Restore the 0136 temporary-table implementation."""
    _restore_browse_v7_from_0136()
    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_V7} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BROWSE_V7} TO ec_app")
