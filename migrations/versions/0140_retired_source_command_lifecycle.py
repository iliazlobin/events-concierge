"""Fence retired-source commands before worker execution.

Revision ID: 0140
Revises: 0139
Create Date: 2026-08-01

Retirement is an immutable registry fact, but a refresh command can have been queued before that
fact was recorded.  Reconcile those commands once, then make claim and renewal re-check the source
lifecycle at their database authority boundaries.  A retired target is retained as terminal,
operator-safe evidence and never handed to a provider adapter.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0140"
down_revision: str | None = "0139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM = "(integer,integer,text,text)"
_RENEW = "(uuid,uuid,integer)"
_ERROR_CONSTRAINT = "ck_ingestion_admin_commands_error_code_v2"


def upgrade() -> None:
    """Terminalize stale retired work and fence every future claim or renewal."""
    _replace_error_constraint(include_source_retired=True)
    _reconcile_retired_commands()
    _create_claim_function(retirement_aware=True)
    _create_renew_function(retirement_aware=True)


def downgrade() -> None:
    """Restore the 0139 command behavior without leaving an unsupported error code."""
    _create_claim_function(retirement_aware=False)
    _create_renew_function(retirement_aware=False)
    op.execute(
        """
        UPDATE public.ingestion_admin_commands
        SET error_code = 'source_refresh_failed'
        WHERE error_code = 'source_retired'
        """
    )
    _replace_error_constraint(include_source_retired=False)


def _replace_error_constraint(*, include_source_retired: bool) -> None:
    """Replace only the command error-vocabulary check, regardless of its legacy name."""
    op.execute(
        """
        DO $$
        DECLARE
            v_constraint_name name;
        BEGIN
            SELECT constraint_row.conname
            INTO v_constraint_name
            FROM pg_catalog.pg_constraint AS constraint_row
            WHERE constraint_row.conrelid =
                    'public.ingestion_admin_commands'::regclass
              AND constraint_row.contype = 'c'
              AND pg_catalog.pg_get_constraintdef(constraint_row.oid)
                    LIKE '%source_refresh_failed%'
            ORDER BY constraint_row.conname
            LIMIT 1;

            IF v_constraint_name IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42704',
                    MESSAGE = 'ingestion admin command error constraint is missing';
            END IF;
            EXECUTE pg_catalog.format(
                'ALTER TABLE public.ingestion_admin_commands DROP CONSTRAINT %I',
                v_constraint_name
            );
        END;
        $$
        """
    )
    retired_code = ", 'source_retired'" if include_source_retired else ""
    op.execute(
        f"""
        ALTER TABLE public.ingestion_admin_commands
        ADD CONSTRAINT {_ERROR_CONSTRAINT}
        CHECK (
            error_code IS NULL
            OR error_code IN (
                'source_refresh_failed',
                'due_refresh_failed',
                'invalid_result',
                'worker_unavailable'{retired_code}
            )
        )
        """
    )


def _reconcile_retired_commands() -> None:
    """Close queued and reclaimable retired-source commands without provider egress."""
    op.execute(
        """
        UPDATE public.ingestion_admin_commands AS command
        SET status = 'failed',
            started_at = COALESCE(command.started_at, clock_timestamp()),
            completed_at = clock_timestamp(),
            lease_token = NULL,
            lease_expires_at = NULL,
            result = NULL,
            error_code = 'source_retired'
        FROM public.catalog_sources AS source
        WHERE command.action = 'refresh_source'
          AND command.source_key = source.source_key
          AND source.retired_at IS NOT NULL
          AND (
              command.status = 'queued'
              OR (
                  command.status = 'running'
                  AND command.lease_expires_at <= clock_timestamp()
              )
          )
        """
    )


def _create_claim_function(*, retirement_aware: bool) -> None:
    """Install the provenance-preserving claim contract with an optional retirement fence."""
    reconciliation = (
        """
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'failed',
                started_at = COALESCE(command.started_at, clock_timestamp()),
                completed_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                result = NULL,
                error_code = 'source_retired'
            FROM public.catalog_sources AS source
            WHERE command.action = 'refresh_source'
              AND command.source_key = source.source_key
              AND source.retired_at IS NOT NULL
              AND (
                  command.status = 'queued'
                  OR (
                      command.status = 'running'
                      AND command.lease_expires_at <= clock_timestamp()
                  )
              );
        """
        if retirement_aware
        else ""
    )
    candidate_fence = (
        """
                AND (
                    command.action <> 'refresh_source'
                    OR EXISTS (
                        SELECT 1
                        FROM public.catalog_sources AS source
                        WHERE source.source_key = command.source_key
                          AND source.retired_at IS NULL
                    )
                )
        """
        if retirement_aware
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_claim_ingestion_admin_commands_v2(
            p_limit integer,
            p_lease_seconds integer,
            p_executor_release_revision text,
            p_executor_image_digest text
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
               OR p_executor_release_revision IS NULL
               OR p_executor_release_revision
                    !~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'
               OR (
                   p_executor_image_digest IS NOT NULL
                   AND p_executor_image_digest !~ '^sha256:[0-9a-f]{{64}}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command claim input is invalid';
            END IF;

            {reconciliation}

            RETURN QUERY
            WITH candidates AS (
                SELECT command.command_id
                FROM public.ingestion_admin_commands AS command
                WHERE (
                    (
                        command.status = 'queued'
                        AND command.available_at <= clock_timestamp()
                    )
                    OR (
                        command.status = 'running'
                        AND command.lease_expires_at <= clock_timestamp()
                    )
                )
                {candidate_fence}
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
                error_code = NULL,
                executor_source_revision = CASE
                    WHEN command.action = 'refresh_source' THEN (
                        SELECT source.source_revision
                        FROM public.catalog_sources AS source
                        WHERE source.source_key = command.source_key
                    )
                    ELSE NULL
                END,
                executor_release_revision = p_executor_release_revision,
                executor_image_digest = p_executor_image_digest
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
    qualified = f"public.fn_claim_ingestion_admin_commands_v2{_CLAIM}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _create_renew_function(*, retirement_aware: bool) -> None:
    """Keep lease renewal exact-token fenced and refuse a newly retired target."""
    retirement_fence = (
        """
              AND (
                  command.action <> 'refresh_source'
                  OR NOT EXISTS (
                      SELECT 1
                      FROM public.catalog_sources AS source
                      WHERE source.source_key = command.source_key
                        AND source.retired_at IS NOT NULL
                  )
              )
        """
        if retirement_aware
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_renew_ingestion_admin_command_lease(
            p_command_id uuid,
            p_lease_token uuid,
            p_lease_seconds integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_command_id IS NULL
               OR p_lease_token IS NULL
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 300
               OR p_lease_seconds > 21600
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command lease renewal input is invalid';
            END IF;

            UPDATE public.ingestion_admin_commands AS command
            SET lease_expires_at =
                    clock_timestamp() + p_lease_seconds * INTERVAL '1 second'
            WHERE command.command_id = p_command_id
              AND command.status = 'running'
              AND command.lease_token = p_lease_token
              AND command.lease_expires_at > clock_timestamp()
              {retirement_fence};
            RETURN FOUND;
        END;
        $$
        """
    )
    qualified = f"public.fn_renew_ingestion_admin_command_lease{_RENEW}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")
