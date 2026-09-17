"""Resolve explicitly provisioned Google identities without opening tenant enumeration.

Revision ID: 0195
Revises: 0194

No identities or accounts are created, linked or rewritten. Only the consumer application role
may resolve an exact versioned Google subject to its internal UUID, before tenant RLS is known.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0195"
down_revision: str | None = "0194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
            ) THEN
                RAISE EXCEPTION '0195 identity lookup requires a BYPASSRLS migration owner';
            END IF;
        END;
        $$;
        CREATE FUNCTION public.fn_resolve_google_tenant(p_subject_binding text)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT tenant.tenant_id
            FROM public.tenants AS tenant
            WHERE p_subject_binding COLLATE "C"
                  ~ '^oidc:v1:https://accounts[.]google[.]com:[!-~]{1,255}$'
              AND tenant.oidc_subject = p_subject_binding
              AND NOT EXISTS (
                  SELECT 1 FROM public.account_erasure_requests AS erasure
                  WHERE erasure.tenant_id = tenant.tenant_id
              )
        $$;
        REVOKE ALL ON FUNCTION public.fn_resolve_google_tenant(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.fn_resolve_google_tenant(text) TO ec_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.fn_resolve_google_tenant(text)")
