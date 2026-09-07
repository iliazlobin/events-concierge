"""Deduplicate active ingestion-admin commands by their execution target.

Revision ID: 0124
Revises: 0123
Create Date: 2026-07-29

Command UUIDs remain immutable replay keys, but independently generated UUIDs must not admit the
same active work from concurrent tabs or API processes. Partial unique indexes reserve one active
slot per source refresh and one fleet-wide due-refresh slot while a command is queued or running.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0124"
down_revision: str | None = "0123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_SOURCE_INDEX = "ux_ingestion_admin_commands_active_source"
_ACTIVE_DUE_INDEX = "ux_ingestion_admin_commands_active_due"


def upgrade() -> None:
    """Reserve active command targets and make every enqueue conflict deterministic."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.ingestion_admin_commands AS command
                WHERE command.action = 'refresh_source'
                  AND command.status IN ('queued', 'running')
                GROUP BY command.source_key
                HAVING count(*) > 1
            )
            OR (
                SELECT count(*) > 1
                FROM public.ingestion_admin_commands AS command
                WHERE command.action = 'refresh_due'
                  AND command.status IN ('queued', 'running')
            )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE =
                        'active ingestion command duplicates must be drained before migration';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_ACTIVE_SOURCE_INDEX}
        ON public.ingestion_admin_commands (source_key)
        WHERE action = 'refresh_source'
          AND status IN ('queued', 'running')
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_ACTIVE_DUE_INDEX}
        ON public.ingestion_admin_commands (action)
        WHERE action = 'refresh_due'
          AND status IN ('queued', 'running')
        """
    )
    _create_enqueue_function(dedupe_active_targets=True)


def downgrade() -> None:
    """Restore UUID-only admission after removing the active-target indexes."""
    _create_enqueue_function(dedupe_active_targets=False)
    op.execute(f"DROP INDEX IF EXISTS public.{_ACTIVE_DUE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS public.{_ACTIVE_SOURCE_INDEX}")


def _create_enqueue_function(*, dedupe_active_targets: bool) -> None:
    """Install the enqueue body used before or after active-target uniqueness."""
    conflict_clause = "ON CONFLICT DO NOTHING" if dedupe_active_targets else (
        "ON CONFLICT (command_id) DO NOTHING"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_enqueue_ingestion_admin_command(
            p_command_id uuid,
            p_action text,
            p_source_key text,
            p_requested_by text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            existing_action text;
            existing_source_key text;
            existing_requested_by text;
            source_present boolean;
            source_available boolean;
            policy_allowed boolean;
        BEGIN
            IF p_command_id IS NULL
               OR p_action NOT IN ('refresh_source', 'refresh_due')
               OR (
                   p_action = 'refresh_source'
                   AND (
                       p_source_key IS NULL
                       OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
                   )
               )
               OR (p_action = 'refresh_due' AND p_source_key IS NOT NULL)
               OR p_requested_by IS NULL
               OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
               OR p_requested_by ~ '[[:cntrl:]]'
            THEN
                RETURN 'invalid';
            END IF;

            SELECT command.action, command.source_key, command.requested_by
            INTO existing_action, existing_source_key, existing_requested_by
            FROM public.ingestion_admin_commands AS command
            WHERE command.command_id = p_command_id;
            IF FOUND THEN
                IF existing_action = p_action
                   AND existing_source_key IS NOT DISTINCT FROM p_source_key
                   AND existing_requested_by = p_requested_by
                THEN
                    RETURN 'replayed';
                END IF;
                RETURN 'conflict';
            END IF;

            IF p_action = 'refresh_source' THEN
                SELECT true,
                       source.enabled
                       AND source.handoff_only
                       AND NOT public.fn_ingestion_admin_source_is_fixture(
                           source.source_key, source.publisher, source.seed_url
                       )
                       AND source.reviewed_at IS NOT NULL
                       AND source.reviewed_at <= clock_timestamp()
                       AND (
                           source.review_expires_at IS NULL
                           OR source.review_expires_at > clock_timestamp()
                       )
                INTO source_present, source_available
                FROM public.catalog_sources AS source
                WHERE source.source_key = p_source_key;
                IF NOT COALESCE(source_present, false) THEN
                    RETURN 'not_found';
                END IF;
                IF NOT COALESCE(source_available, false) THEN
                    RETURN 'unavailable';
                END IF;
            END IF;

            SELECT policy.source IS NOT NULL
                   AND NOT policy.quarantined
                   AND COALESCE(
                       (policy.automation_allowed ->> 'browser')::boolean,
                       false
                   )
            INTO policy_allowed
            FROM (SELECT 1) AS singleton
            LEFT JOIN public.source_policy AS policy
              ON policy.source = 'public_jsonld';
            IF NOT COALESCE(policy_allowed, false) THEN
                RETURN 'policy_blocked';
            END IF;

            INSERT INTO public.ingestion_admin_commands (
                command_id, action, source_key, requested_by
            )
            VALUES (p_command_id, p_action, p_source_key, p_requested_by)
            {conflict_clause};
            IF FOUND THEN
                RETURN 'enqueued';
            END IF;

            SELECT command.action, command.source_key, command.requested_by
            INTO existing_action, existing_source_key, existing_requested_by
            FROM public.ingestion_admin_commands AS command
            WHERE command.command_id = p_command_id;
            IF FOUND
               AND existing_action = p_action
               AND existing_source_key IS NOT DISTINCT FROM p_source_key
               AND existing_requested_by = p_requested_by
            THEN
                RETURN 'replayed';
            END IF;
            RETURN 'conflict';
        END;
        $$
        """
    )
