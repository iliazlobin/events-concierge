"""Suggest public catalog names before limiting results, using the browse eligibility rules.

Revision ID: 0210
Revises: 0209
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0210"
down_revision: str | None = "0209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUGGEST = (
    "public.fn_suggest_catalog_names_v1"
    "(text,text[],timestamp with time zone[],timestamp with time zone[],text[],text[],text,"
    "integer,integer,text[],text,integer)"
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_suggest_catalog_names_v1(
            p_query text, p_source_keys text[],
            p_window_starts timestamptz[], p_window_ends timestamptz[],
            p_cities text[], p_location_scopes text[], p_price text,
            p_price_max_cents integer, p_price_min_cents integer,
            p_topics text[], p_availability text, p_limit integer
        )
        RETURNS TABLE (name text, kinds text[], event_count bigint)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_query text := lower(btrim(p_query));
        BEGIN
            IF p_query IS NULL OR length(btrim(p_query)) NOT BETWEEN 2 AND 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 20
               OR cardinality(p_window_starts) IS NULL
               OR cardinality(p_window_starts) NOT BETWEEN 1 AND 8
               OR cardinality(p_window_ends) IS DISTINCT FROM cardinality(p_window_starts)
               OR (p_availability IS NOT NULL
                   AND p_availability NOT IN ('available', 'sold_out'))
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'catalog name suggestion query is invalid';
            END IF;

            RETURN QUERY
            WITH eligible AS MATERIALIZED (
                -- This private primitive owns source admission, fixtures, cancellations,
                -- latest-success/retained-history lanes and all scalar filter validation.
                -- No chronological page or description search may hide a matching name.
                SELECT DISTINCT candidate.canonical_event_id, candidate.title,
                       candidate.venue_name, candidate.organizer_name, candidate.host_names,
                       candidate.speaker_names, candidate.partner_names, candidate.entity_profiles
                FROM unnest(p_window_starts, p_window_ends) AS selected_window(start_at, end_at)
                CROSS JOIN LATERAL
                    public.fn_browse_filtered_current_catalog_events_unbounded_v1(
                        p_source_keys, selected_window.start_at, selected_window.end_at, NULL, p_cities,
                        p_location_scopes, p_price, p_price_max_cents, p_price_min_cents,
                        p_topics, 'soonest', NULL, NULL, NULL
                    ) AS candidate
                WHERE p_availability IS NULL
                   OR (p_availability = 'available' AND candidate.registration_status = 'open')
                   OR (p_availability = 'sold_out' AND candidate.registration_status = 'sold_out')
            ), mentions AS (
                SELECT event.canonical_event_id,
                       left(btrim(mention.name), 160) AS display_name,
                       mention.kind, event.entity_profiles
                FROM eligible AS event
                CROSS JOIN LATERAL (
                    SELECT event.title AS name, 'event'::text AS kind
                    UNION ALL SELECT event.venue_name, 'venue'
                    UNION ALL SELECT event.organizer_name, 'organizer'
                    UNION ALL SELECT value, 'host' FROM unnest(event.host_names) AS value
                    UNION ALL SELECT value, 'speaker' FROM unnest(event.speaker_names) AS value
                    UNION ALL SELECT value, 'partner' FROM unnest(event.partner_names) AS value
                ) AS mention
                WHERE nullif(btrim(mention.name), '') IS NOT NULL
                  AND mention.name !~ '[\x00-\x1f\x7f]'
                  AND strpos(lower(left(btrim(mention.name), 160)), v_query) > 0
            ), typed_mentions AS (
                SELECT mention.canonical_event_id, mention.display_name, mention.kind
                FROM mentions AS mention
                UNION ALL
                SELECT mention.canonical_event_id, mention.display_name, profile.value->>'kind'
                FROM mentions AS mention
                CROSS JOIN LATERAL jsonb_array_elements(mention.entity_profiles) AS profile(value)
                WHERE mention.kind IN ('organizer', 'host', 'speaker', 'partner')
                  AND lower(btrim(profile.value->>'name')) = lower(mention.display_name)
                  AND profile.value->>'role' = mention.kind
                  AND profile.value->>'kind' IN ('person', 'organization')
            ), names AS (
                -- Grouping is a text-search choice, not an entity identity merge.
                SELECT lower(mention.display_name) AS name_key,
                       min(mention.display_name COLLATE "C") AS display_name,
                       array_agg(DISTINCT mention.kind ORDER BY mention.kind) AS kinds,
                       count(DISTINCT mention.canonical_event_id) AS event_count
                FROM typed_mentions AS mention
                GROUP BY lower(mention.display_name)
            )
            SELECT names.display_name, names.kinds, names.event_count
            FROM names
            ORDER BY CASE WHEN names.name_key = v_query THEN 0
                          WHEN strpos(names.name_key, v_query) = 1 THEN 1 ELSE 2 END,
                     names.event_count DESC, names.name_key COLLATE "C"
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SUGGEST} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SUGGEST} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_SUGGEST}")
