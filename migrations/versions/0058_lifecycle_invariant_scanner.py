"""Expose a read-only, cross-tenant ADR-007 lifecycle invariant snapshot.

Revision ID: 0058
Revises: 0057
Create Date: 2026-07-18

The application role deliberately remains a non-owner, non-``BYPASSRLS`` role.  A global
invariant scan cannot run through ordinary RLS reads: an unset tenant GUC must correctly see zero
tenant rows.  These narrow SECURITY DEFINER functions are therefore an intentional observability
capability, not a relaxation of application-table grants.  The migration refuses to install them
unless its owner can bypass FORCE RLS (a superuser or a role with ``BYPASSRLS``); otherwise a
global scan could falsely report a clean zero-row result.  At runtime ``ec_app`` receives EXECUTE
only.  The snapshot returns aggregate counts and the liveness worklist returns only deterministic
workflow identifiers, never tenant contact data, event text, URLs, payloads, or credentials.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_SIGNATURE = "()"
_WORKLIST_SIGNATURE = "(text, integer)"


def upgrade() -> None:
    """Install aggregate-only scanner functions behind a privileged definer boundary.

    The migration owner becomes the SECURITY DEFINER owner.  It must be a superuser or possess
    ``BYPASSRLS`` in every deployment because ``lifecycle`` and ``handoff_tasks`` are FORCE-RLS
    tables.  Failing loudly here is safer than allowing a later app-role call without tenant
    context to mistake RLS's required zero rows for a clean global invariant result (FR-1.3/1.4,
    ADR-007/008).
    """
    _require_bypassrls_definer()
    op.execute(
        """
        CREATE FUNCTION public.fn_lifecycle_invariant_snapshot()
        RETURNS TABLE (
            active_lifecycles_missing_watch bigint,
            active_lifecycles_mismatched_watch bigint,
            terminal_watch_subscriptions bigint,
            orphan_watch_subscriptions bigint,
            orphan_watch_registry bigint,
            active_handoffs_missing_expiry_queue bigint,
            orphan_active_handoff_tasks bigint,
            inactive_handoff_expiry_queue bigint,
            pending_watch_register_projections bigint,
            pending_watch_unregister_projections bigint,
            invalid_lifecycle_workflow_identity_count bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH watchable_lifecycles AS (
                SELECT lifecycle.lifecycle_id,
                       lifecycle.tenant_id,
                       lifecycle.canonical_event_id,
                       lifecycle.workflow_id,
                       lifecycle.registration_source
                FROM public.lifecycle AS lifecycle
                WHERE lifecycle.state IN ('registered', 'scheduled', 'reconciled')
            ),
            active_handoff_tasks AS (
                SELECT task.task_id,
                       task.tenant_id,
                       task.workflow_id,
                       task.canonical_event_id,
                       task.expiry_transition_id
                FROM public.handoff_tasks AS task
                WHERE task.state IN ('open', 'notified')
            )
            SELECT
                (
                    SELECT count(*)
                    FROM watchable_lifecycles AS lifecycle
                    WHERE lifecycle.registration_source IS NULL
                       OR NOT EXISTS (
                           SELECT 1
                           FROM public.watch_subscriptions AS subscription
                           WHERE subscription.canonical_event_id = lifecycle.canonical_event_id
                             AND subscription.source = lifecycle.registration_source
                             AND subscription.tenant_id = lifecycle.tenant_id
                             AND subscription.workflow_id = lifecycle.workflow_id
                       )
                ) AS active_lifecycles_missing_watch,
                (
                    SELECT count(DISTINCT lifecycle.lifecycle_id)
                    FROM watchable_lifecycles AS lifecycle
                    JOIN public.watch_subscriptions AS subscription
                      ON subscription.canonical_event_id = lifecycle.canonical_event_id
                     AND subscription.tenant_id = lifecycle.tenant_id
                     AND subscription.workflow_id = lifecycle.workflow_id
                    WHERE subscription.source IS DISTINCT FROM lifecycle.registration_source
                ) AS active_lifecycles_mismatched_watch,
                (
                    SELECT count(*)
                    FROM public.watch_subscriptions AS subscription
                    JOIN public.lifecycle AS lifecycle
                      ON lifecycle.tenant_id = subscription.tenant_id
                     AND lifecycle.workflow_id = subscription.workflow_id
                     AND lifecycle.canonical_event_id = subscription.canonical_event_id
                    WHERE lifecycle.state IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
                ) AS terminal_watch_subscriptions,
                (
                    SELECT count(*)
                    FROM public.watch_subscriptions AS subscription
                    LEFT JOIN public.lifecycle AS lifecycle
                      ON lifecycle.tenant_id = subscription.tenant_id
                     AND lifecycle.workflow_id = subscription.workflow_id
                     AND lifecycle.canonical_event_id = subscription.canonical_event_id
                    WHERE lifecycle.lifecycle_id IS NULL
                ) AS orphan_watch_subscriptions,
                (
                    SELECT count(*)
                    FROM public.watch_registry AS registry
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.watch_subscriptions AS subscription
                        WHERE subscription.canonical_event_id = registry.canonical_event_id
                          AND subscription.source = registry.source
                    )
                ) AS orphan_watch_registry,
                (
                    SELECT count(*)
                    FROM active_handoff_tasks AS task
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.handoff_expiry_queue AS expiry
                        WHERE expiry.task_id = task.task_id
                          AND expiry.tenant_id = task.tenant_id
                          AND expiry.workflow_id = task.workflow_id
                          AND expiry.canonical_event_id = task.canonical_event_id
                          AND expiry.expiry_transition_id = task.expiry_transition_id
                          AND expiry.resolved_at IS NULL
                    )
                ) AS active_handoffs_missing_expiry_queue,
                (
                    SELECT count(*)
                    FROM active_handoff_tasks AS task
                    LEFT JOIN public.lifecycle AS lifecycle
                      ON lifecycle.tenant_id = task.tenant_id
                     AND lifecycle.workflow_id = task.workflow_id
                     AND lifecycle.canonical_event_id = task.canonical_event_id
                     AND lifecycle.state NOT IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
                    WHERE lifecycle.lifecycle_id IS NULL
                ) AS orphan_active_handoff_tasks,
                (
                    SELECT count(*)
                    FROM public.handoff_expiry_queue AS expiry
                    LEFT JOIN public.handoff_tasks AS task ON task.task_id = expiry.task_id
                    WHERE expiry.resolved_at IS NULL
                      AND (
                          task.task_id IS NULL
                          OR task.state NOT IN ('open', 'notified')
                      )
                ) AS inactive_handoff_expiry_queue,
                (
                    SELECT count(*)
                    FROM public.lifecycle_watch_projection_outbox AS projection
                    WHERE projection.action = 'register'
                      AND projection.delivered_at IS NULL
                ) AS pending_watch_register_projections,
                (
                    SELECT count(*)
                    FROM public.lifecycle_watch_projection_outbox AS projection
                    WHERE projection.action = 'unregister'
                      AND projection.delivered_at IS NULL
                ) AS pending_watch_unregister_projections,
                (
                    SELECT count(*)
                    FROM public.lifecycle AS lifecycle
                    WHERE lifecycle.workflow_id IS DISTINCT FROM (
                        lifecycle.tenant_id::text || ':' || lifecycle.canonical_event_id::text
                    )
                ) AS invalid_lifecycle_workflow_identity_count
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_nonterminal_lifecycle_workflow_ids(
            p_after_workflow_id text,
            p_limit integer
        )
        RETURNS TABLE (workflow_id text)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle liveness worklist limit must be between 1 and 1000';
            END IF;
            IF p_after_workflow_id IS NOT NULL
               AND NULLIF(btrim(p_after_workflow_id), '') IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle liveness worklist cursor must be non-empty when supplied';
            END IF;

            RETURN QUERY
            SELECT lifecycle.workflow_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.state NOT IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
              AND (
                  p_after_workflow_id IS NULL
                  OR lifecycle.workflow_id > p_after_workflow_id
              )
            ORDER BY lifecycle.workflow_id
            LIMIT p_limit;
        END;
        $$
        """
    )
    for signature, name in (
        (_SNAPSHOT_SIGNATURE, "fn_lifecycle_invariant_snapshot"),
        (_WORKLIST_SIGNATURE, "fn_list_nonterminal_lifecycle_workflow_ids"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{name}{signature} TO ec_app")


def downgrade() -> None:
    """Remove only the read-only invariant-observability capability."""
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_list_nonterminal_lifecycle_workflow_ids(text, integer)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_lifecycle_invariant_snapshot()")


def _require_bypassrls_definer() -> None:
    """Reject a deployment whose future function owner cannot read FORCE-RLS rows globally."""
    op.execute(
        """
        DO $$
        DECLARE
            v_can_bypass boolean;
        BEGIN
            SELECT role.rolsuper OR role.rolbypassrls
            INTO v_can_bypass
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user;
            IF COALESCE(v_can_bypass, false) IS NOT TRUE THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = '0058 lifecycle invariant functions require a superuser or BYPASSRLS migration owner';
            END IF;
        END;
        $$
        """
    )
