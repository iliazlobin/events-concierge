"""Add lease-fenced command heartbeats and authoritative overview run counters.

Revision ID: 0130
Revises: 0129
Create Date: 2026-07-30

Command workers now use a short recovery lease and renew it while a bounded source or fleet refresh
is active. A crashed worker therefore becomes reclaimable within one lease window instead of
stranding the active command target for hours.

Operational precondition: cadence and ingestion-command workers MUST be stopped and drained before
this migration is applied. The one-time reconciliation expires live pre-heartbeat command leases so
the new worker can reclaim them; an old worker that is still executing will be fenced from recording
completion. Downgrade intentionally does not attempt to restore reconciled lease timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0130"
down_revision: str | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENEW_SIGNATURE = "(uuid,uuid,integer)"
_OVERVIEW_V2_SIGNATURE = "()"


def upgrade() -> None:
    """Install exact-token heartbeats, normalized overview counters, and reconcile old leases."""
    op.execute(
        """
        CREATE FUNCTION public.fn_renew_ingestion_admin_command_lease(
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
              AND command.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        f"public.fn_renew_ingestion_admin_command_lease{_RENEW_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        f"public.fn_renew_ingestion_admin_command_lease{_RENEW_SIGNATURE} TO ec_app"
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_overview_v2()
        RETURNS TABLE (
            generated_at timestamptz,
            policy_allowed boolean,
            policy_reason text,
            policy_code text,
            sources bigint,
            active_sources bigint,
            due_sources bigint,
            running_runs bigint,
            failed_runs_24h bigint,
            catalog_events bigint,
            pending_commands bigint,
            fixture_sources bigint,
            latest_success_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH legacy AS MATERIALIZED (
                SELECT *
                FROM public.fn_get_ingestion_admin_overview()
            ), normalized_runs AS MATERIALIZED (
                SELECT facts.*
                FROM public.fn_ingestion_admin_run_facts_v2(
                    false,
                    NULL,
                    NULL
                ) AS facts
            ), annotated_runs AS MATERIALIZED (
                SELECT normalized_runs.*,
                       (
                           row_number() OVER (
                               PARTITION BY normalized_runs.source_key
                               ORDER BY
                                   normalized_runs.started_at DESC,
                                   normalized_runs.run_key DESC
                           ) = 1
                       ) AS is_latest_for_source
                FROM normalized_runs
            ), run_summary AS (
                SELECT count(*) FILTER (
                           WHERE annotated_runs.status = 'running'
                       ) AS running_count,
                       count(*) FILTER (
                           WHERE annotated_runs.is_latest_for_source
                             AND annotated_runs.status = 'failed'
                             AND COALESCE(
                                 annotated_runs.completed_at,
                                 annotated_runs.started_at
                             ) >= legacy.generated_at - INTERVAL '24 hours'
                       ) AS unresolved_failed_count
                FROM annotated_runs
                CROSS JOIN legacy
            )
            SELECT legacy.generated_at,
                   legacy.policy_allowed,
                   legacy.policy_reason,
                   legacy.policy_code,
                   legacy.sources,
                   legacy.active_sources,
                   legacy.due_sources,
                   run_summary.running_count,
                   run_summary.unresolved_failed_count,
                   legacy.catalog_events,
                   legacy.pending_commands,
                   legacy.fixture_sources,
                   legacy.latest_success_at
            FROM legacy
            CROSS JOIN run_summary
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        f"public.fn_get_ingestion_admin_overview_v2{_OVERVIEW_V2_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        f"public.fn_get_ingestion_admin_overview_v2{_OVERVIEW_V2_SIGNATURE} TO ec_app"
    )

    # Workers must be drained before upgrade: this deliberately fences every still-live
    # pre-heartbeat command owner and makes the command immediately reclaimable by the new worker.
    op.execute(
        """
        UPDATE public.ingestion_admin_commands AS command
        SET lease_expires_at = clock_timestamp()
        WHERE command.status = 'running'
          AND command.lease_token IS NOT NULL
          AND (
              command.lease_expires_at IS NULL
              OR command.lease_expires_at > clock_timestamp()
          )
        """
    )


def downgrade() -> None:
    """Remove additive capabilities without pretending reconciled lease timestamps are reversible."""
    op.execute(
        "REVOKE ALL ON FUNCTION "
        f"public.fn_get_ingestion_admin_overview_v2{_OVERVIEW_V2_SIGNATURE} FROM ec_app"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        f"public.fn_get_ingestion_admin_overview_v2{_OVERVIEW_V2_SIGNATURE}"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        f"public.fn_renew_ingestion_admin_command_lease{_RENEW_SIGNATURE} FROM ec_app"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        f"public.fn_renew_ingestion_admin_command_lease{_RENEW_SIGNATURE}"
    )
