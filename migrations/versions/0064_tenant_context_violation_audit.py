"""Audit an app-managed missing tenant context without weakening FORCE-RLS.

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-18

PostgreSQL has no SELECT trigger, and an RLS predicate is evaluated per plan row, so a policy-side
effect cannot reliably record an omitted-context query (and would roll back with a rejected write).
The explicit tenant-session boundary therefore commits a PII-free private event before yielding its
empty-GUC transaction. This preserves FR-1.4/AC-3's fail-closed result without treating intentional
tenant-neutral catalog/control-plane work as a violation (FR-1.3, NFR-7, ADR-001).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Install the owner-only, no-argument missing-context audit capability."""
    op.execute(
        """
        CREATE TABLE public.tenant_context_violation_audit (
            violation_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            violation_type text NOT NULL
                           CHECK (violation_type = 'missing_tenant_context'),
            caller_role    text NOT NULL CHECK (caller_role = 'ec_app'),
            occurred_at    timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE public.tenant_context_violation_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_context_violation_audit FROM ec_app")
    op.execute(
        "REVOKE ALL ON SEQUENCE public.tenant_context_violation_audit_violation_id_seq FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON SEQUENCE public.tenant_context_violation_audit_violation_id_seq FROM ec_app"
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_record_missing_tenant_context()
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'missing-context audit requires an empty tenant context';
            END IF;
            IF session_user <> 'ec_app' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'missing-context audit caller is not permitted';
            END IF;

            INSERT INTO public.tenant_context_violation_audit (violation_type, caller_role)
            VALUES ('missing_tenant_context', session_user);
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_record_missing_tenant_context() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.fn_record_missing_tenant_context() TO ec_app")


def downgrade() -> None:
    """Remove the isolated private audit capability and its PII-free evidence rows."""
    op.execute("DROP FUNCTION IF EXISTS public.fn_record_missing_tenant_context()")
    op.execute("DROP TABLE IF EXISTS public.tenant_context_violation_audit")
