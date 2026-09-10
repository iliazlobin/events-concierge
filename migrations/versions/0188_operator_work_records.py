"""Restricted pending request starts and notification work records.

Revision ID: 0188
Revises: 0187

Expose bounded scheduling and diagnostic metadata through the existing restricted
definer. No tenant payload, topic, recipient or raw failure text is projected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0188"
down_revision: str | None = "0187"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_get_operator_records_v1(text,text,integer,integer,text)"
_DEFINER = "ec_operator_aggregate_definer"


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, "
        "ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
    # 0182 already grants this NOLOGIN owner SELECT on these two outboxes. Runtime
    # operators gain no table privileges and cannot become the definer role.
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
CREATE FUNCTION public.fn_get_operator_records_v1(
    p_queue text, p_scope text DEFAULT 'pending', p_offset integer DEFAULT 0,
    p_limit integer DEFAULT 10, p_record_id text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE v_request_id uuid; v_notification_id bigint; v_result jsonb;
BEGIN
    IF p_queue IS NULL OR p_queue NOT IN ('request_start', 'notifications')
       OR p_scope IS NULL
       OR (p_queue = 'request_start' AND p_scope NOT IN ('pending', 'errors'))
       OR (p_queue = 'notifications' AND p_scope NOT IN ('pending', 'failed'))
       OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 10000
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator record query';
    END IF;
    IF p_record_id IS NOT NULL THEN
        IF p_queue = 'request_start' THEN
            IF p_record_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator record query';
            END IF;
            v_request_id := p_record_id::uuid;
        ELSE
            IF p_record_id !~ '^[1-9][0-9]{0,18}$' THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator record query';
            END IF;
            IF p_record_id::numeric > 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator record query';
            END IF;
            v_notification_id := p_record_id::bigint;
        END IF;
    END IF;

    WITH raw_facts AS MATERIALIZED (
        SELECT request_id::text AS record_id, request_id AS request_order,
               NULL::bigint AS notification_order, created_at, next_attempt_at,
               lease_expires_at, attempt_count, last_error, NULL::timestamptz AS failed_at
        FROM public.request_start_outbox
        WHERE p_queue = 'request_start' AND started_at IS NULL
          AND (p_scope = 'pending' OR last_error IS NOT NULL)
          AND (v_request_id IS NULL OR request_id = v_request_id)
        UNION ALL
        SELECT id::text, NULL::uuid, id, created_at, next_attempt_at,
               lease_expires_at, attempt_count, last_error, failed_at
        FROM public.outbox
        WHERE p_queue = 'notifications'
          AND ((p_scope = 'pending' AND delivered_at IS NULL AND failed_at IS NULL)
               OR (p_scope = 'failed' AND failed_at IS NOT NULL))
          AND (v_notification_id IS NULL OR id = v_notification_id)
    ), facts AS MATERIALIZED (
        SELECT *,
            CASE WHEN failed_at IS NOT NULL THEN 'failed'
                 WHEN lease_expires_at > statement_timestamp() THEN 'leased'
                 WHEN next_attempt_at > statement_timestamp() THEN 'scheduled'
                 ELSE 'ready' END AS state,
            CASE WHEN last_error IS NULL AND failed_at IS NULL THEN NULL
                 WHEN p_queue = 'request_start' THEN
                    CASE WHEN last_error = 'fresh request-start retry after reclaim'
                         THEN 'test_retry_fixture' ELSE 'unclassified' END
                 ELSE CASE last_error
                    WHEN 'notification materialization or delivery failed' THEN 'delivery_failed'
                    WHEN 'outbox projection contains a forbidden plaintext completion URL' THEN 'unsafe_projection'
                    WHEN '0106 quarantined an unreconstructable plaintext completion capability' THEN 'unsafe_projection'
                    WHEN 'outbox topic has no notification or audit-only classification' THEN 'unsupported_topic'
                    WHEN 'completion-capable handoff has no protected completion URL' THEN 'missing_protected_capability'
                    WHEN 'notification ledger lease was lost' THEN 'lease_lost'
                    WHEN 'notification ledger is leased by another relay' THEN 'delivery_busy'
                    ELSE 'unclassified' END
            END AS error_code
        FROM raw_facts
    ), page AS (
        SELECT * FROM facts ORDER BY created_at, request_order, notification_order
        LIMIT p_limit OFFSET p_offset
    )
    SELECT jsonb_build_object(
        'generated_at', statement_timestamp(), 'queue', p_queue, 'scope', p_scope,
        'offset', p_offset, 'limit', p_limit, 'total', (SELECT count(*) FROM facts),
        'items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'record_id', record_id,
            'label', CASE WHEN p_queue = 'request_start' THEN 'Request start' ELSE 'Notification work' END,
            'state', state, 'error_code', error_code,
            'error_summary', CASE error_code
                WHEN 'test_retry_fixture' THEN 'fresh request-start retry after reclaim'
                WHEN 'delivery_failed' THEN 'Notification materialization or delivery failed.'
                WHEN 'unsafe_projection' THEN 'Notification work was quarantined because it contained an unsafe plaintext capability.'
                WHEN 'unsupported_topic' THEN 'Outbox work has no notification or audit-only classification.'
                WHEN 'missing_protected_capability' THEN 'Notification work lacks the required protected completion capability.'
                WHEN 'lease_lost' THEN 'The notification ledger lease was lost.'
                WHEN 'delivery_busy' THEN 'The notification ledger is leased by another worker.'
                WHEN 'unclassified' THEN CASE WHEN last_error IS NULL THEN 'No failure reason was recorded.'
                    ELSE 'An error was recorded. Raw error text is unavailable in this view.' END
                ELSE NULL END,
            'attempt_count', attempt_count,
            'attempt_kind', CASE WHEN p_queue = 'request_start' THEN 'failed_start_attempts' ELSE 'failed_delivery_attempts' END,
            'created_at', created_at,
            'next_attempt_at', CASE WHEN state <> 'failed' THEN next_attempt_at END,
            'lease_expires_at', CASE WHEN state <> 'failed' THEN lease_expires_at END,
            'failed_at', failed_at, 'last_observed_at', failed_at, 'sources', '[]'::jsonb
        ) ORDER BY created_at, request_order, notification_order) FROM page), '[]'::jsonb)
    ) INTO v_result;
    RETURN v_result;
END
$$;
"""
