"""Persist tenant-authored display facts separately from tenant identity.

Revision ID: 0157
Revises: 0156
Create Date: 2026-08-26

``public.tenants`` is insert-once identity: 0061 revoked table DML from ``ec_app`` and made
``fn_provision_tenant`` the sole write path, deliberately so an application compromise cannot rebind
an OIDC subject, a notification address, or a RelayInbox.  Display name and time zone are neither
identity nor authority -- they are user-authored presentation -- so they land in a satellite table
the application may write directly, and the identity guarantee is left exactly as it was.

The address the concierge sends to is therefore *not* here.  ``tenants.notify_email`` remains the
single source of truth for delivery, because it carries bearer capability links (FR-6.6, ADR-011);
changing it requires proof of control over the new mailbox and is not a profile edit.

``revision`` mirrors the tenant ranking profile's monotonic contract so the whole account surface
uses one concurrency idiom (FR-1.2--FR-1.4, ADR-001).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0157"
down_revision: str | None = "0156"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create a FORCE-RLS, tenant-writable store for user-authored display facts."""
    op.execute(
        """
        CREATE TABLE public.tenant_profiles (
            tenant_id     uuid PRIMARY KEY
                          REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            display_name  text,
            time_zone     text,
            revision      bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT tenant_profiles_display_name_valid CHECK (
                display_name IS NULL
                OR (
                    display_name = btrim(display_name)
                    AND char_length(display_name) BETWEEN 1 AND 64
                    AND display_name !~ '[[:cntrl:]]'
                )
            ),
            CONSTRAINT tenant_profiles_time_zone_valid CHECK (
                time_zone IS NULL OR time_zone ~ '^[A-Za-z0-9+_/-]{1,64}$'
            )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_reject_tenant_profile_revision_regression()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.revision < OLD.revision THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'tenant profile revision cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tenant_profiles_revision_monotonic
        BEFORE UPDATE ON public.tenant_profiles
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_reject_tenant_profile_revision_regression()
        """
    )
    op.execute("ALTER TABLE public.tenant_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_profiles FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_profiles_tenant_isolation
        ON public.tenant_profiles
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.tenant_profiles FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tenant_profiles TO ec_app"
    )


def downgrade() -> None:
    """Drop the profile store, its policy, and its revision guard."""
    op.execute("DROP POLICY IF EXISTS tenant_profiles_tenant_isolation ON public.tenant_profiles")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tenant_profiles_revision_monotonic ON public.tenant_profiles"
    )
    op.execute("DROP TABLE IF EXISTS public.tenant_profiles")
    op.execute("DROP FUNCTION IF EXISTS public.fn_reject_tenant_profile_revision_regression()")
