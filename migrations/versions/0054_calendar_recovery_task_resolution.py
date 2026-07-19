"""Resolve an obsolete calendar-recovery task when factual scheduling resumes.

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cancel only a resolved calendar-recovery task in the guarded lifecycle transaction.

    A calendar write failure deliberately leaves a factual RSVP in ``registered`` with a manual
    ``calendar_write_failed`` task.  A later successful calendar write or organizer reschedule
    must retire that particular task and its orphan-expiry instruction atomically with the state
    transition; otherwise a stale task can later expire a healthy scheduled lifecycle (FR-6.6,
    FR-8.7, ADR-007/008).
    """
    op.execute(
        """
        CREATE FUNCTION public.fn_resolve_calendar_recovery_handoff()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL
               OR NEW.tenant_id IS DISTINCT FROM v_context_tenant_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'calendar-recovery task resolution requires its tenant context';
            END IF;

            IF OLD.state = 'registered' AND NEW.state IN ('scheduled', 'reconciled') THEN
                WITH resolved AS (
                    UPDATE public.handoff_tasks AS task
                    SET state = 'cancelled'
                    WHERE task.tenant_id = NEW.tenant_id
                      AND task.workflow_id = NEW.workflow_id
                      AND task.canonical_event_id = NEW.canonical_event_id
                      AND task.reason = 'calendar_write_failed'
                      AND task.state IN ('open', 'notified')
                    RETURNING task.task_id
                )
                UPDATE public.handoff_expiry_queue AS expiry
                SET resolved_at = clock_timestamp(),
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = 'calendar recovery resolved by lifecycle transition'
                FROM resolved
                WHERE expiry.task_id = resolved.task_id
                  AND expiry.resolved_at IS NULL;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_resolve_calendar_recovery_handoff() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER tr_lifecycle_resolve_calendar_recovery_handoff
        AFTER UPDATE OF state ON public.lifecycle
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_resolve_calendar_recovery_handoff()
        """
    )


def downgrade() -> None:
    """Remove the recovery-task cleanup hook before rolling back the terminal request guard."""
    op.execute(
        "DROP TRIGGER IF EXISTS tr_lifecycle_resolve_calendar_recovery_handoff ON public.lifecycle"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_resolve_calendar_recovery_handoff()")
