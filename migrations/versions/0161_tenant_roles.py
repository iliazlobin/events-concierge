"""Record which accounts hold an operator role.

Revision ID: 0161
Revises: 0160
Create Date: 2026-08-26

A role is authority, not presentation, so it deliberately does *not* live in
``public.tenant_profiles`` -- that table is application-writable, and a self-service write path to a
role column is a self-promotion primitive: any bug or injection reaching the profile update would
mint an operator.  This table therefore copies the identity posture from 0061: ``ec_app`` may read
it and nothing more.  Roles are granted out of band by an operator with database credentials.

Absence of a row means ``member``.  The table stores only elevated grants, so the common account
costs nothing and the privileged set is enumerable in one scan.

Scope note: this drives whether the product surfaces operator affordances to a signed-in user.  It
is not by itself the access control on the ingestion administration API, which is separately
restricted to loopback callers -- a UI that hides a link is a courtesy, never a boundary.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0161"
down_revision: str | None = "0160"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create a read-only-to-the-application role table under FORCE RLS."""
    op.execute(
        """
        CREATE TABLE public.tenant_roles (
            tenant_id   uuid PRIMARY KEY
                        REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            role        text NOT NULL CHECK (role IN ('admin', 'operator')),
            granted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
            granted_by  text
        )
        """
    )
    op.execute("ALTER TABLE public.tenant_roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_roles FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_roles_tenant_isolation
        ON public.tenant_roles
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    # The application may observe its own role and may never assign one.  Mirrors 0061's treatment
    # of tenant identity: a compromised request path cannot escalate what it can only SELECT.
    op.execute("REVOKE ALL ON TABLE public.tenant_roles FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_roles FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.tenant_roles TO ec_app")


def downgrade() -> None:
    """Drop the role table and its policy."""
    op.execute("DROP POLICY IF EXISTS tenant_roles_tenant_isolation ON public.tenant_roles")
    op.execute("DROP TABLE IF EXISTS public.tenant_roles")
