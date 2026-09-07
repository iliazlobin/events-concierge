"""Link fleet commands to their cadence run keys and expose safe live command detail.

Revision ID: 0142
Revises: 0141
Create Date: 2026-08-02

Cadence run keys remain source/slot identities so repeated scheduler passes preserve their existing
idempotency.  A separate append-only association records which durable admin command observed and
dispatched each slot.  The read capabilities expose only bounded operational fields and normalized
run evidence; lease tokens, raw payloads, stack traces, and provider response text stay private.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0142"
down_revision: str | None = "0141"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINK = "(uuid,integer,uuid,text,text,integer)"
_CHILDREN = "(uuid)"


def upgrade() -> None:
    """Install authoritative command/run associations and v3 command projections."""
    op.execute(
        """
        CREATE TABLE public.ingestion_admin_command_runs (
            command_id uuid NOT NULL
                REFERENCES public.ingestion_admin_commands(command_id) ON DELETE CASCADE,
            command_attempt integer NOT NULL,
            source_key text NOT NULL
                REFERENCES public.catalog_sources(source_key) ON DELETE RESTRICT,
            run_key text NOT NULL,
            position integer NOT NULL,
            linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (command_id, command_attempt, source_key, run_key),
            UNIQUE (command_id, command_attempt, position),
            CONSTRAINT ck_admin_command_runs_attempt_positive CHECK (command_attempt >= 1),
            CHECK (position BETWEEN 0 AND 499),
            CHECK (run_key ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_ingestion_admin_command_runs_run
        ON public.ingestion_admin_command_runs (
            source_key, run_key, command_id, command_attempt
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE public.ingestion_admin_command_runs FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.ingestion_admin_command_runs FROM ec_app")

    op.execute(
        """
        CREATE FUNCTION public.fn_link_ingestion_admin_command_run_v1(
            p_command_id uuid,
            p_command_attempt integer,
            p_lease_token uuid,
            p_source_key text,
            p_run_key text,
            p_position integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            command_action text;
        BEGIN
            IF p_command_id IS NULL
               OR p_command_attempt IS NULL OR p_command_attempt < 1
               OR p_lease_token IS NULL
               OR p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_position IS NULL OR p_position NOT BETWEEN 0 AND 499
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion command run link input is invalid';
            END IF;

            SELECT command.action
              INTO command_action
             FROM public.ingestion_admin_commands AS command
             WHERE command.command_id = p_command_id
               AND command.status = 'running'
               AND command.attempt_count = p_command_attempt
               AND command.lease_token = p_lease_token
               AND command.lease_expires_at > statement_timestamp();

            IF command_action IS NULL THEN
                RETURN false;
            END IF;
            IF command_action = 'refresh_source' AND NOT EXISTS (
                SELECT 1
                  FROM public.ingestion_admin_commands AS command
                 WHERE command.command_id = p_command_id
                   AND command.source_key = p_source_key
                   AND p_run_key = 'admin:' || command.command_id::text
                   AND p_position = 0
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'refresh_source command run link does not match its immutable input';
            END IF;
            IF command_action = 'refresh_due'
               AND p_run_key NOT LIKE 'cadence:' || p_source_key || ':%'
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'refresh_due command run link is not a cadence source slot';
            END IF;

            INSERT INTO public.ingestion_admin_command_runs (
                command_id, command_attempt, source_key, run_key, position
            ) VALUES (
                p_command_id, p_command_attempt, p_source_key, p_run_key, p_position
            )
            ON CONFLICT (command_id, command_attempt, source_key, run_key) DO NOTHING;

            RETURN EXISTS (
                SELECT 1
                  FROM public.ingestion_admin_command_runs AS link
                 WHERE link.command_id = p_command_id
                   AND link.command_attempt = p_command_attempt
                   AND link.source_key = p_source_key
                   AND link.run_key = p_run_key
                   AND link.position = p_position
            );
        END;
        $$
        """
    )

    _create_command_projections()
    _create_child_projection()

    for signature in (
        f"public.fn_link_ingestion_admin_command_run_v1{_LINK}",
        "public.fn_list_ingestion_admin_commands_v3(integer)",
        "public.fn_get_ingestion_admin_command_v3(uuid)",
        f"public.fn_list_ingestion_admin_command_runs_v1{_CHILDREN}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove command detail capabilities without changing cadence run identity."""
    for signature in (
        f"public.fn_list_ingestion_admin_command_runs_v1{_CHILDREN}",
        "public.fn_get_ingestion_admin_command_v3(uuid)",
        "public.fn_list_ingestion_admin_commands_v3(integer)",
        f"public.fn_link_ingestion_admin_command_run_v1{_LINK}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP INDEX IF EXISTS public.ix_ingestion_admin_command_runs_run")
    op.execute("DROP TABLE IF EXISTS public.ingestion_admin_command_runs")


def _create_command_projections() -> None:
    columns = """
        command_id uuid,
        action text,
        source_key text,
        status text,
        requested_at timestamptz,
        started_at timestamptz,
        completed_at timestamptz,
        result jsonb,
        error_code text,
        source_revision integer,
        release_revision text,
        image_digest text,
        executor_source_revision integer,
        executor_release_revision text,
        executor_image_digest text,
        requested_by text,
        attempt_count integer,
        available_at timestamptz,
        lease_expires_at timestamptz,
        worker_state text
    """
    select_columns = """
        command.command_id,
        command.action,
        command.source_key,
        command.status,
        command.requested_at,
        command.started_at,
        command.completed_at,
        command.result,
        command.error_code,
        command.source_revision,
        command.release_revision,
        command.image_digest,
        command.executor_source_revision,
        command.executor_release_revision,
        command.executor_image_digest,
        command.requested_by,
        command.attempt_count,
        command.available_at,
        command.lease_expires_at,
        CASE
            WHEN command.status = 'queued' AND command.available_at > statement_timestamp()
                THEN 'retry_scheduled'
            WHEN command.status = 'queued' THEN 'awaiting_claim'
            WHEN command.status = 'running'
                 AND command.lease_expires_at > statement_timestamp()
                THEN 'heartbeat_live'
            WHEN command.status = 'running' THEN 'lease_expired'
            WHEN command.status = 'completed' THEN 'finished'
            WHEN command.status = 'failed' THEN 'failed'
            ELSE 'failed'
        END
    """
    op.execute(
        f"""
        CREATE FUNCTION public.fn_list_ingestion_admin_commands_v3(p_limit integer)
        RETURNS TABLE ({columns})
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command limit is invalid';
            END IF;
            RETURN QUERY
            SELECT {select_columns}
              FROM public.ingestion_admin_commands AS command
             ORDER BY command.requested_at DESC, command.command_id
             LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.fn_get_ingestion_admin_command_v3(p_command_id uuid)
        RETURNS TABLE ({columns})
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT {select_columns}
              FROM public.ingestion_admin_commands AS command
             WHERE p_command_id IS NOT NULL
               AND command.command_id = p_command_id
        $$
        """
    )


def _create_child_projection() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_command_runs_v1(p_command_id uuid)
        RETURNS TABLE (
            run_position integer,
            source_key text,
            display_name text,
            run_key text,
            status text,
            phase text,
            linked_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error_code text,
            attempt_count integer,
            duration_ms bigint,
            updated_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT link.position AS run_position,
                   link.source_key,
                   source.display_name,
                   link.run_key,
                   COALESCE(refresh.status, 'pending') AS status,
                   CASE COALESCE(refresh.status, 'pending')
                       WHEN 'pending' THEN 'awaiting_dispatch'
                       WHEN 'running' THEN 'collecting'
                       WHEN 'paused' THEN 'deferred'
                       WHEN 'succeeded' THEN 'completed'
                       WHEN 'failed' THEN 'failed'
                       ELSE 'failed'
                   END AS phase,
                   link.linked_at,
                   refresh.started_at,
                   refresh.completed_at,
                   refresh.candidate_count,
                   refresh.canonical_count,
                   public.fn_normalize_catalog_refresh_error(refresh.error) AS error_code,
                   refresh.attempt_count,
                   CASE
                       WHEN refresh.started_at IS NULL THEN NULL
                       ELSE GREATEST(
                           0,
                           FLOOR(EXTRACT(EPOCH FROM (
                               COALESCE(refresh.completed_at, statement_timestamp())
                               - refresh.started_at
                           )) * 1000)::bigint
                       )
                   END AS duration_ms,
                   COALESCE(refresh.completed_at, refresh.started_at, link.linked_at) AS updated_at
              FROM public.ingestion_admin_command_runs AS link
              JOIN public.ingestion_admin_commands AS command
                ON command.command_id = link.command_id
               AND command.attempt_count = link.command_attempt
              JOIN public.catalog_sources AS source
                ON source.source_key = link.source_key
              LEFT JOIN public.catalog_refresh_runs AS refresh
                ON refresh.source_key = link.source_key
               AND refresh.run_key = link.run_key
             WHERE p_command_id IS NOT NULL
               AND link.command_id = p_command_id
             ORDER BY link.position, link.source_key, link.run_key
             LIMIT 500
        $$
        """
    )
