"""Align command/run linkage with the unbounded command reclaim lifecycle.

Revision ID: 0143
Revises: 0142
Create Date: 2026-08-02

Command claims increment an integer attempt counter whenever an expired lease is reclaimed.  The
command/run association must accept every attempt the claim capability can produce; a smaller
storage or projection cap can otherwise make an otherwise valid command permanently unreadable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0143"
down_revision: str | None = "0142"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINK = "(uuid,integer,uuid,text,text,integer)"
_TABLE = "public.ingestion_admin_command_runs"
_LEGACY_AUTO_CONSTRAINT = "ingestion_admin_command_runs_command_attempt_check"
_POSITIVE_CONSTRAINT = "ck_admin_command_runs_attempt_positive"
_BOUNDED_CONSTRAINT = "ck_admin_command_runs_attempt_1_50"


def upgrade() -> None:
    """Remove the artificial retry ceiling without weakening lease fencing."""
    _replace_attempt_constraint(
        name=_POSITIVE_CONSTRAINT,
        expression="command_attempt >= 1",
    )
    _replace_link_function(attempt_guard="p_command_attempt < 1")


def downgrade() -> None:
    """Restore the former cap only when retained evidence makes that lossless."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM {_TABLE}
                 WHERE command_attempt > 50
            ) OR EXISTS (
                SELECT 1
                  FROM public.ingestion_admin_commands
                 WHERE attempt_count > 50
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'cannot restore command attempt cap while attempts above 50 exist';
            END IF;
        END;
        $$
        """
    )
    _replace_attempt_constraint(
        name=_BOUNDED_CONSTRAINT,
        expression="command_attempt BETWEEN 1 AND 50",
    )
    _replace_link_function(attempt_guard="p_command_attempt NOT BETWEEN 1 AND 50")


def _replace_attempt_constraint(*, name: str, expression: str) -> None:
    for constraint in (
        _LEGACY_AUTO_CONSTRAINT,
        _POSITIVE_CONSTRAINT,
        _BOUNDED_CONSTRAINT,
    ):
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({expression})")


def _replace_link_function(*, attempt_guard: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_link_ingestion_admin_command_run_v1(
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
               OR p_command_attempt IS NULL OR {attempt_guard}
               OR p_lease_token IS NULL
               OR p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{{0,255}}$'
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
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_link_ingestion_admin_command_run_v1{_LINK} FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_link_ingestion_admin_command_run_v1{_LINK} TO ec_app"
    )
