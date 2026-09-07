"""Add durable delayed retries to the ingestion-admin command queue.

Revision ID: 0110
Revises: 0109
Create Date: 2026-07-23

Deferred commands remain in the durable queue but are not claimable until their database-clock
availability time. The defer transition is fenced by the active, unexpired lease and preserves
the attempt count so retry history cannot be erased.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_SIGNATURE = "(integer,integer)"
_DEFER_SIGNATURE = "(uuid,uuid,integer)"


def upgrade() -> None:
    """Install database-clock scheduling and a lease-fenced defer transition."""
    op.execute(
        """
        ALTER TABLE public.ingestion_admin_commands
        ADD COLUMN available_at timestamptz NOT NULL DEFAULT now()
        """
    )
    op.execute("DROP INDEX public.ix_ingestion_admin_commands_claim")
    op.execute(
        """
        CREATE INDEX ix_ingestion_admin_commands_claim
        ON public.ingestion_admin_commands (
            status,
            available_at,
            lease_expires_at,
            requested_at,
            command_id
        )
        WHERE status IN ('queued', 'running')
        """
    )
    _create_due_aware_claim_function()
    _create_defer_function()

    for signature in (
        f"public.fn_claim_ingestion_admin_commands{_CLAIM_SIGNATURE}",
        f"public.fn_defer_ingestion_admin_command{_DEFER_SIGNATURE}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Restore immediate queue claiming and remove delayed-retry state."""
    op.execute(
        f"REVOKE ALL ON FUNCTION "
        f"public.fn_defer_ingestion_admin_command{_DEFER_SIGNATURE} FROM ec_app"
    )
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_defer_ingestion_admin_command{_DEFER_SIGNATURE}")
    _create_immediate_claim_function()
    op.execute(
        f"REVOKE ALL ON FUNCTION "
        f"public.fn_claim_ingestion_admin_commands{_CLAIM_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"public.fn_claim_ingestion_admin_commands{_CLAIM_SIGNATURE} TO ec_app"
    )

    op.execute("DROP INDEX public.ix_ingestion_admin_commands_claim")
    op.execute(
        """
        ALTER TABLE public.ingestion_admin_commands
        DROP COLUMN available_at
        """
    )
    op.execute(
        """
        CREATE INDEX ix_ingestion_admin_commands_claim
        ON public.ingestion_admin_commands (requested_at, command_id)
        WHERE status IN ('queued', 'running')
        """
    )


def _create_due_aware_claim_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.fn_claim_ingestion_admin_commands(
            p_limit integer,
            p_lease_seconds integer
        )
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            attempt_count integer,
            lease_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 300 OR p_lease_seconds > 21600
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command claim input is invalid';
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT command.command_id
                FROM public.ingestion_admin_commands AS command
                WHERE (
                        command.status = 'queued'
                        AND command.available_at <= clock_timestamp()
                    )
                   OR (
                       command.status = 'running'
                       AND command.lease_expires_at <= clock_timestamp()
                   )
                ORDER BY command.requested_at, command.command_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'running',
                started_at = COALESCE(command.started_at, clock_timestamp()),
                attempt_count = command.attempt_count + 1,
                lease_token = gen_random_uuid(),
                lease_expires_at =
                    clock_timestamp() + p_lease_seconds * INTERVAL '1 second',
                result = NULL,
                error_code = NULL
            FROM candidates
            WHERE command.command_id = candidates.command_id
            RETURNING command.command_id,
                      command.action,
                      command.source_key,
                      command.attempt_count,
                      command.lease_token;
        END;
        $$
        """
    )


def _create_defer_function() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_defer_ingestion_admin_command(
            p_command_id uuid,
            p_lease_token uuid,
            p_retry_after_seconds integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_command_id IS NULL
               OR p_lease_token IS NULL
               OR p_retry_after_seconds IS NULL
               OR p_retry_after_seconds < 1
               OR p_retry_after_seconds > 21600
            THEN
                RETURN false;
            END IF;

            UPDATE public.ingestion_admin_commands AS command
            SET status = 'queued',
                available_at =
                    clock_timestamp() + p_retry_after_seconds * INTERVAL '1 second',
                started_at = NULL,
                completed_at = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                result = NULL,
                error_code = NULL
            WHERE command.command_id = p_command_id
              AND command.status = 'running'
              AND command.lease_token = p_lease_token
              AND command.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )


def _create_immediate_claim_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.fn_claim_ingestion_admin_commands(
            p_limit integer,
            p_lease_seconds integer
        )
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            attempt_count integer,
            lease_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 300 OR p_lease_seconds > 21600
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command claim input is invalid';
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT command.command_id
                FROM public.ingestion_admin_commands AS command
                WHERE command.status = 'queued'
                   OR (
                       command.status = 'running'
                       AND command.lease_expires_at <= clock_timestamp()
                   )
                ORDER BY command.requested_at, command.command_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'running',
                started_at = COALESCE(command.started_at, clock_timestamp()),
                attempt_count = command.attempt_count + 1,
                lease_token = gen_random_uuid(),
                lease_expires_at =
                    clock_timestamp() + p_lease_seconds * INTERVAL '1 second',
                result = NULL,
                error_code = NULL
            FROM candidates
            WHERE command.command_id = candidates.command_id
            RETURNING command.command_id,
                      command.action,
                      command.source_key,
                      command.attempt_count,
                      command.lease_token;
        END;
        $$
        """
    )
