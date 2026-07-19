"""Restrict tenant identity rows to RLS reads and one provisioning capability.

Revision ID: 0061
Revises: 0060
Create Date: 2026-07-18

Tenant identity used to be the one pre-RLS onboarding exception.  This migration makes its
ordinary reads tenant scoped and removes application-role table mutation.  Onboarding instead
receives one fixed-search-path, exact-replay provisioning capability: it may bind one supplied
tenant id, OIDC subject, notification address, and inbound-only RelayInbox exactly once.  It
does not expose which existing binding conflicted (FR-1.2--FR-1.5, ADR-001, ADR-011).  Because
the capability provisions before an RLS context exists, its definer must bypass FORCE RLS.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(uuid, text, text, text)"
_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Make tenant identity a FORCE-RLS read model with a narrow onboarding write path."""
    _require_bypassrls_definer()
    op.execute("ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenants_tenant_isolation
        ON public.tenants
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )

    # The application can inspect only its RLS-visible identity.  It cannot create, alter, or
    # delete identity bindings through table DML; provisioning below is the sole capability.
    op.execute("REVOKE ALL ON TABLE public.tenants FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenants FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.tenants TO ec_app")
    _create_provision_function()
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_provision_tenant{_SIGNATURE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_provision_tenant{_SIGNATURE} FROM ec_app")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.fn_provision_tenant{_SIGNATURE} TO ec_app")


def downgrade() -> None:
    """Restore the pre-0061 onboarding/admin table behavior and application grants."""
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_provision_tenant{_SIGNATURE}")
    op.execute("DROP POLICY IF EXISTS tenants_tenant_isolation ON public.tenants")
    op.execute("ALTER TABLE public.tenants NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenants DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.tenants FROM ec_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tenants TO ec_app")


def _create_provision_function() -> None:
    """Create the tenant-binding capability with exact replay and generic conflict errors.

    Advisory locks cover each immutable binding and are acquired in deterministic order.  That
    serializes concurrent same-binding calls so a retry returns false rather than racing an
    insert.  Hash collisions only over-serialize unrelated provisioning requests; they cannot
    merge or disclose identities.
    """
    op.execute(
        """
        CREATE FUNCTION public.fn_provision_tenant(
            p_tenant_id uuid,
            p_oidc_subject text,
            p_notify_email text,
            p_relay_inbox text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_existing_tenant_id uuid;
            v_existing_oidc_subject text;
            v_existing_notify_email text;
            v_existing_relay_inbox text;
            v_lock_key bigint;
        BEGIN
            IF p_tenant_id IS NULL
               OR p_oidc_subject IS NULL
               OR NULLIF(btrim(p_oidc_subject), '') IS NULL
               OR p_notify_email IS NULL
               OR NULLIF(btrim(p_notify_email), '') IS NULL
               OR p_relay_inbox IS NULL
               OR NULLIF(btrim(p_relay_inbox), '') IS NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'tenant provisioning input is incomplete or malformed';
            END IF;

            -- Take the three unique bindings in one order so duplicate delivery converges cleanly
            -- and overlapping requests cannot deadlock each other.
            FOR v_lock_key IN
                SELECT DISTINCT hashtextextended(binding.value, 0)
                FROM unnest(
                    ARRAY[
                        'tenant:' || p_tenant_id::text,
                        'subject:' || p_oidc_subject,
                        'relay:' || p_relay_inbox
                    ]
                ) AS binding(value)
                ORDER BY 1
            LOOP
                PERFORM pg_advisory_xact_lock(v_lock_key);
            END LOOP;

            SELECT tenant.tenant_id,
                   tenant.oidc_subject,
                   tenant.notify_email,
                   tenant.relay_inbox
            INTO v_existing_tenant_id,
                 v_existing_oidc_subject,
                 v_existing_notify_email,
                 v_existing_relay_inbox
            FROM public.tenants AS tenant
            WHERE tenant.tenant_id = p_tenant_id;
            IF FOUND THEN
                IF v_existing_oidc_subject IS NOT DISTINCT FROM p_oidc_subject
                   AND v_existing_notify_email IS NOT DISTINCT FROM p_notify_email
                   AND v_existing_relay_inbox IS NOT DISTINCT FROM p_relay_inbox
                THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'tenant identity binding conflicts with an existing tenant';
            END IF;

            -- The supplied tenant id is unbound, but either globally unique external binding may
            -- already belong to another tenant.  Deliberately return one generic error so no
            -- caller learns whether the subject or RelayInbox, nor any existing value, matched.
            IF EXISTS (
                SELECT 1
                FROM public.tenants AS tenant
                WHERE tenant.oidc_subject = p_oidc_subject
                   OR tenant.relay_inbox = p_relay_inbox
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'tenant identity binding conflicts with an existing tenant';
            END IF;

            BEGIN
                INSERT INTO public.tenants (
                    tenant_id,
                    oidc_subject,
                    notify_email,
                    relay_inbox
                )
                VALUES (
                    p_tenant_id,
                    p_oidc_subject,
                    p_notify_email,
                    p_relay_inbox
                );
            EXCEPTION
                WHEN unique_violation THEN
                    -- A writer outside this capability can still race while an operator performs
                    -- emergency work.  Do not leak PostgreSQL's key/value diagnostic.
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'tenant identity binding conflicts with an existing tenant';
            END;
            RETURN true;
        END;
        $$
        """
    )


def _require_bypassrls_definer() -> None:
    """Reject a deployment whose future provisioning-function owner cannot bypass FORCE RLS.

    Provisioning is intentionally called before the new tenant has an established
    `app.tenant_id` GUC.  A SECURITY DEFINER owner without superuser or `BYPASSRLS` would see
    zero rows and fail its INSERT under FORCE RLS, turning a successful migration into a broken
    onboarding path.  The migration owner becomes the function owner, so reject that deployment
    before installing either the policy or capability.
    """
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
                    MESSAGE = '0061 tenant provisioning function requires a superuser or BYPASSRLS migration owner';
            END IF;
        END;
        $$
        """
    )
