"""Persist ADR-008's delayed direct-reconciliation safety net.

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store one repair after a definitive closed-workflow fanout failure (ADR-008)."""
    op.execute(
        """
        CREATE TABLE lifecycle_organizer_change_ledger (
            tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            workflow_id    text NOT NULL,
            -- A reconciliation can be delivered directly by a fixture/manual repair as well as
            -- the normal detector.  Keep this durable idempotency key independent from retention
            -- of the public event_changes ledger; the delayed repair queue itself remains FK-bound.
            fingerprint    text NOT NULL,
            applied_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, workflow_id, fingerprint)
        )
        """
    )
    # Calendar writes precede the guarded lifecycle transition.  This outbox trigger records the
    # durable organizer-change identity in the same transaction as that transition's notification.
    # A retry with a different transition id can still idempotently upsert/delete the calendar, but
    # it cannot emit a second lifecycle notification.  The 15-minute repair worker reads this
    # ledger before it calls reconciliation, so it normally avoids even the redundant calendar call.
    op.execute(
        """
        CREATE FUNCTION public.fn_dedup_lifecycle_organizer_change_outbox()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_fingerprint text;
            v_workflow_id text;
            v_inserted_fingerprint text;
        BEGIN
            IF NEW.topic NOT IN ('lifecycle.cancelled', 'lifecycle.reconciled') THEN
                RETURN NEW;
            END IF;
            v_fingerprint := NULLIF(NEW.payload ->> 'organizer_change_fingerprint', '');
            v_workflow_id := NULLIF(NEW.payload ->> 'workflow_id', '');
            IF v_fingerprint IS NULL OR v_workflow_id IS NULL THEN
                RETURN NEW;
            END IF;

            INSERT INTO public.lifecycle_organizer_change_ledger
                (tenant_id, workflow_id, fingerprint)
            VALUES (NEW.tenant_id, v_workflow_id, v_fingerprint)
            ON CONFLICT DO NOTHING
            RETURNING fingerprint INTO v_inserted_fingerprint;
            IF v_inserted_fingerprint IS NULL THEN
                RETURN NULL;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_dedup_lifecycle_organizer_change_outbox() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER tr_dedup_lifecycle_organizer_change_outbox
        BEFORE INSERT ON public.outbox
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_dedup_lifecycle_organizer_change_outbox()
        """
    )
    op.execute(
        """
        CREATE TABLE event_change_calendar_repairs (
            repair_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            fingerprint        text NOT NULL REFERENCES event_changes(fingerprint) ON DELETE CASCADE,
            tenant_id          uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            workflow_id        text NOT NULL,
            created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
            next_attempt_at    timestamptz NOT NULL
                               DEFAULT (clock_timestamp() + INTERVAL '15 minutes'),
            repaired_at        timestamptz,
            attempt_count      integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            lease_token        text,
            lease_expires_at   timestamptz,
            last_error         text,
            UNIQUE (fingerprint, tenant_id, workflow_id),
            CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_event_change_calendar_repairs_ready
        ON event_change_calendar_repairs (next_attempt_at, repair_id)
        WHERE repaired_at IS NULL
        """
    )
    # This is an opaque global control queue, like event_change_deliveries.  The worker has no
    # tenant payload other than the id it must re-enter through the guarded reconciliation service.
    op.execute("REVOKE ALL ON TABLE public.event_change_calendar_repairs FROM ec_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.event_change_calendar_repairs TO ec_app"
    )
    op.execute("REVOKE ALL ON TABLE public.lifecycle_organizer_change_ledger FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.lifecycle_organizer_change_ledger TO ec_app")


def downgrade() -> None:
    """Remove only delayed repair control records; normal lifecycle truth is unchanged."""
    op.execute("DROP TRIGGER IF EXISTS tr_dedup_lifecycle_organizer_change_outbox ON public.outbox")
    op.execute("DROP FUNCTION IF EXISTS public.fn_dedup_lifecycle_organizer_change_outbox()")
    op.execute("DROP TABLE IF EXISTS public.lifecycle_organizer_change_ledger")
    op.execute("DROP TABLE IF EXISTS public.event_change_calendar_repairs")
