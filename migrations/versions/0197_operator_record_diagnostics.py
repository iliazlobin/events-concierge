"""Inspect one retained work error through a bounded, redacted operator read.

Revision ID: 0197
Revises: 0196

No queue rows or retry schedules change. Ordinary record lists keep their existing
allowlisted summaries; this separate read requires an exact reference and scope.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0197"
down_revision: str | None = "0196"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_get_operator_record_diagnostic_v1(text,text,text)"
_DEFINER = "ec_operator_aggregate_definer"


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, "
        "ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
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


_PROJECTION = r"""
CREATE FUNCTION public.fn_get_operator_record_diagnostic_v1(
    p_queue text, p_scope text, p_record_id text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_reference text;
    v_error text;
    v_message text;
    v_redacted boolean := false;
    v_truncated boolean := false;
    v_pattern text;
BEGIN
    IF p_queue IS NULL OR p_queue NOT IN ('request_start', 'notifications')
       OR p_scope IS NULL
       OR (p_queue = 'request_start' AND p_scope NOT IN ('pending', 'errors'))
       OR (p_queue = 'notifications' AND p_scope NOT IN ('pending', 'failed'))
       OR p_record_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator diagnostic query';
    END IF;
    IF p_queue = 'request_start' THEN
        IF p_record_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator diagnostic query';
        END IF;
        SELECT request_id::text, last_error INTO v_reference, v_error
        FROM public.request_start_outbox
        WHERE request_id = p_record_id::uuid AND started_at IS NULL
          AND (p_scope = 'pending' OR last_error IS NOT NULL);
    ELSE
        IF p_record_id !~ '^(0|-?[1-9][0-9]{0,18})$' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator diagnostic query';
        END IF;
        IF p_record_id::numeric NOT BETWEEN -9223372036854775808 AND 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator diagnostic query';
        END IF;
        SELECT id::text, last_error INTO v_reference, v_error
        FROM public.outbox
        WHERE id = p_record_id::bigint
          AND ((p_scope = 'pending' AND delivered_at IS NULL AND failed_at IS NULL)
               OR (p_scope = 'failed' AND failed_at IS NOT NULL));
    END IF;
    IF v_reference IS NULL THEN RETURN NULL; END IF;

    -- Legacy errors are untrusted. Never project payloads, recipients or lease
    -- fields. Mask secret assignments, credentials, URLs and email addresses in
    -- the message before it crosses the restricted database boundary.
    v_truncated := COALESCE(length(v_error) > 8192, false);
    IF v_truncated THEN
        -- Do not cut a quoted credential in half and defeat its redaction.
        v_message := 'The recorded error exceeds the 8192-character inspection limit.';
    ELSE
        v_message := v_error;
        FOREACH v_pattern IN ARRAY ARRAY[
            $pattern$-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*$pattern$,
            $pattern$\m(authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=][^\n\r]*$pattern$,
            $pattern$\m([a-z0-9_]*token|api[_-]?key|password|passwd|secret|client[_-]?secret|otp|magic[_-]?link|recipient|email|payload|raw[_-]?text|body)["']?\s*[:=]\s*("([^"\\]|\\.)*"|'([^'\\]|\\.)*'|[^\n\r]*)$pattern$,
            $pattern$\mbearer\s+[^\s,;"']+$pattern$,
            $pattern$[a-z][a-z0-9+.-]*://[^\s<>"']+$pattern$,
            $pattern$[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}$pattern$,
            $pattern$\meyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+$pattern$
        ] LOOP
            v_message := regexp_replace(v_message, v_pattern, '[REDACTED]', 'gi');
        END LOOP;
        v_redacted := v_message IS DISTINCT FROM v_error;
        -- Strip terminal escape sequences and non-printing controls, preserving
        -- line breaks and indentation. The Web client renders this as text.
        v_message := regexp_replace(v_message, E'\x1b\\[[0-?]*[ -/]*[@-~]', '', 'g');
        v_message := regexp_replace(v_message, E'[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]', '', 'g');
    END IF;
    RETURN jsonb_build_object(
        'generated_at', statement_timestamp(), 'queue', p_queue, 'scope', p_scope,
        'record_id', v_reference, 'message', v_message,
        'redacted', v_redacted, 'truncated', v_truncated,
        'retention', 'last_error_only'
    );
END
$$;
"""
