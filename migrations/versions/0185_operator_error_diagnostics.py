"""Narrow operator diagnostics for request starts and entity refresh evidence.

Revision ID: 0185
Revises: 0184

Raw request errors never leave this restricted definer. No tenant payload or table
privilege is added to operator roles.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0185"
down_revision: str | None = "0184"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_get_operator_errors_v1(text,integer,integer,uuid)"
_DEFINER = "ec_operator_aggregate_definer"


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, "
        "ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
    # Reuse the restricted NOLOGIN owner and its existing SELECT inventory from 0182.
    # The runtime viewer receives only this fixed projection, never raw queue access.
    bind = op.get_bind()
    owner = bind.dialect.identifier_preparer.quote(
        bind.execute(text("SELECT current_user")).scalar_one()
    )
    op.execute(f"GRANT {_DEFINER} TO {owner}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_DEFINER}")
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {_DEFINER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_DEFINER}")
    op.execute(f"REVOKE {_DEFINER} FROM {owner}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_SIGNATURE}")


_PROJECTION = """
CREATE FUNCTION public.fn_get_operator_errors_v1(
    p_queue text, p_offset integer DEFAULT 0, p_limit integer DEFAULT 10,
    p_record_id uuid DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_result jsonb;
BEGIN
    IF p_queue IS NULL OR p_queue NOT IN ('request_start', 'entity_refresh')
       OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 10000
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator error query';
    END IF;
    IF p_queue = 'request_start' THEN
        WITH facts AS MATERIALIZED (
            SELECT request_id AS record_id, created_at, next_attempt_at,
                   lease_expires_at, attempt_count,
                   CASE WHEN lease_expires_at > statement_timestamp() THEN 'leased'
                        WHEN next_attempt_at > statement_timestamp() THEN 'scheduled'
                        ELSE 'ready' END AS state,
                   CASE WHEN last_error = 'fresh request-start retry after reclaim'
                        THEN 'test_retry_fixture' ELSE 'unclassified' END AS error_code,
                   CASE WHEN last_error = 'fresh request-start retry after reclaim'
                        THEN 'fresh request-start retry after reclaim'
                        ELSE 'An error was recorded. Raw error text is unavailable in this view.'
                   END AS error_summary
            FROM public.request_start_outbox
            WHERE started_at IS NULL AND last_error IS NOT NULL
              AND (p_record_id IS NULL OR request_id = p_record_id)
        ), page AS (
            SELECT * FROM facts ORDER BY created_at, record_id LIMIT p_limit OFFSET p_offset
        )
        SELECT jsonb_build_object(
            'total', (SELECT count(*) FROM facts),
            'items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'record_id', record_id, 'label', 'Request start', 'state', state,
                'error_code', error_code, 'error_summary', error_summary,
                'attempt_count', attempt_count, 'created_at', created_at,
                'next_attempt_at', next_attempt_at, 'lease_expires_at', lease_expires_at,
                'last_observed_at', NULL, 'sources', '[]'::jsonb
            ) ORDER BY created_at, record_id) FROM page), '[]'::jsonb)
        ) INTO v_result;
    ELSE
        WITH eligible AS MATERIALIZED (
            SELECT entity.entity_id, entity.display_name, entity.created_at,
                   min(source.next_refresh_at) AS next_refresh_at,
                   max(source.updated_at) AS last_observed_at
            FROM public.catalog_entities entity
            JOIN public.catalog_entity_external_sources source USING (entity_id)
            WHERE entity.identity_status = 'profile_verified'
              AND entity.canonical_profile_url IS NOT NULL
              AND (p_record_id IS NULL OR entity.entity_id = p_record_id)
            GROUP BY entity.entity_id
            HAVING bool_or(source.status IN ('failed', 'blocked'))
        ), page AS (
            SELECT * FROM eligible ORDER BY created_at, entity_id LIMIT p_limit OFFSET p_offset
        ), items AS (
            SELECT page.*,
                   (SELECT jsonb_agg(jsonb_build_object(
                       'source_id', source.source_id,
                       'provider_key', CASE WHEN source.provider_key ~ '^[a-z][a-z0-9_]{0,63}$'
                                            THEN source.provider_key ELSE 'unknown_provider' END,
                       'status', source.status,
                       'error_code', CASE WHEN source.error_code IN ('unavailable', 'rate_limited',
                           'invalid_response', 'unsupported_profile', 'network_policy')
                           THEN source.error_code ELSE 'unclassified' END,
                       'error_summary', CASE source.error_code
                           WHEN 'unavailable' THEN 'Public source was unavailable.'
                           WHEN 'rate_limited' THEN 'Public source rate limit was reached.'
                           WHEN 'invalid_response' THEN 'Public source returned an invalid response.'
                           WHEN 'unsupported_profile' THEN 'This profile source is unsupported.'
                           WHEN 'network_policy' THEN 'Network policy blocked this source.'
                           ELSE 'A source error was recorded; details are unavailable.' END,
                       'observed_at', source.updated_at,
                       'next_refresh_at', source.next_refresh_at
                   ) ORDER BY source.provider_key, source.source_id)
                    FROM public.catalog_entity_external_sources source
                    WHERE source.entity_id = page.entity_id AND source.status IN ('failed', 'blocked')
                   ) AS sources
            FROM page
        )
        SELECT jsonb_build_object(
            'total', (SELECT count(*) FROM eligible),
            'items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'record_id', entity_id, 'label', display_name,
                'state', CASE WHEN next_refresh_at <= statement_timestamp() THEN 'due' ELSE 'scheduled' END,
                'error_code', 'source_errors',
                'error_summary', 'Entity profile has recorded source errors.',
                'attempt_count', NULL, 'created_at', created_at,
                'next_attempt_at', next_refresh_at, 'lease_expires_at', NULL,
                'last_observed_at', last_observed_at, 'sources', sources
            ) ORDER BY created_at, entity_id) FROM items), '[]'::jsonb)
        ) INTO v_result;
    END IF;
    RETURN jsonb_build_object('generated_at', statement_timestamp(), 'queue', p_queue,
        'offset', p_offset, 'limit', p_limit) || v_result;
END $$;
"""
