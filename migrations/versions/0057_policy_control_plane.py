"""Install the durable, fail-closed ADR-004 policy control plane.

Revision ID: 0057
Revises: 0056
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GLOBAL_SIGNATURE = "(boolean)"
_TENANT_SIGNATURE = "(uuid, boolean)"
_SOURCE_SIGNATURE = "(text, jsonb, boolean, boolean, text)"


def upgrade() -> None:
    """Make source/quarantine and global/per-tenant freezes owner-controlled policy data."""
    op.execute(
        """
        CREATE FUNCTION public.fn_policy_automation_allowed_is_valid(p_value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT jsonb_typeof(p_value) = 'object'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_each(p_value) AS entry(modality, enabled)
                   WHERE entry.modality NOT IN ('api', 'browser')
                      OR jsonb_typeof(entry.enabled) <> 'boolean'
               )
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE public.source_policy
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        """
    )
    op.execute(
        """
        ALTER TABLE public.source_policy
            ADD CONSTRAINT source_policy_automation_allowed_valid
            CHECK (public.fn_policy_automation_allowed_is_valid(automation_allowed))
        """
    )
    op.execute(
        """
        ALTER TABLE public.source_policy
            ADD CONSTRAINT source_policy_signed_agent_mode_valid
            CHECK (signed_agent_mode IN ('none', 'present-if-honored', 'required'))
        """
    )
    # These are the existing ratified launch defaults, now seeded into the durable store. Existing
    # operator-owned rows win, so applying this migration never silently reverses a live policy.
    op.execute(
        """
        INSERT INTO public.source_policy (
            source, automation_allowed, paid_allowed, quarantined, signed_agent_mode
        )
        VALUES
            ('meetup', '{"api": true, "browser": false}'::jsonb, false, false, 'none'),
            ('luma', '{"api": false, "browser": true}'::jsonb, false, false, 'none'),
            ('eventbrite', '{}'::jsonb, false, false, 'none'),
            ('ticketmaster', '{}'::jsonb, false, false, 'none'),
            ('serpapi', '{}'::jsonb, false, false, 'none'),
            ('public_jsonld', '{}'::jsonb, false, false, 'none'),
            ('partiful', '{}'::jsonb, false, true, 'none')
        ON CONFLICT (source) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE public.policy_global_control (
            singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            kill_switch     boolean NOT NULL DEFAULT false,
            updated_at      timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute(
        """
        INSERT INTO public.policy_global_control (singleton, kill_switch)
        VALUES (true, false)
        """
    )
    op.execute(
        """
        CREATE TABLE public.tenant_policy_control (
            tenant_id       uuid PRIMARY KEY REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            kill_switch     boolean NOT NULL DEFAULT false,
            updated_at      timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("ALTER TABLE public.tenant_policy_control ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_policy_control FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_policy_control_tenant_isolation
            ON public.tenant_policy_control
            USING (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        """
    )
    op.execute(
        "CREATE INDEX ix_tenant_policy_control_tenant ON public.tenant_policy_control (tenant_id)"
    )

    # All mutations are owner-only. The application role can read a fresh snapshot but cannot turn
    # off a quarantine or freeze itself (FR-7.2, FR-10.3, ADR-004).
    op.execute("REVOKE ALL ON TABLE public.source_policy FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.policy_global_control FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.tenant_policy_control FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.source_policy TO ec_app")
    op.execute("GRANT SELECT ON TABLE public.policy_global_control TO ec_app")
    op.execute("GRANT SELECT ON TABLE public.tenant_policy_control TO ec_app")

    # Empty notifications are advisory only: the immediate guard performs a fresh durable read.
    # They establish the privacy-safe channel a future <=2s listener cache can consume without
    # emitting tenant, source, or policy values in PostgreSQL notification payloads.
    op.execute(
        """
        CREATE FUNCTION public.fn_notify_policy_control_changed()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            PERFORM pg_notify('ec_policy_changed', '');
            RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    for table in ("source_policy", "policy_global_control", "tenant_policy_control"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_notify_policy_control_changed
            AFTER INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH STATEMENT
            EXECUTE FUNCTION public.fn_notify_policy_control_changed()
            """
        )

    op.execute(
        """
        CREATE FUNCTION public.fn_set_policy_global_kill_switch(p_engaged boolean)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_engaged IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'policy global kill-switch value is required';
            END IF;
            UPDATE public.policy_global_control
            SET kill_switch = p_engaged,
                updated_at = clock_timestamp()
            WHERE singleton = true;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'policy global control row is missing';
            END IF;
            RETURN p_engaged;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_set_tenant_policy_kill_switch(
            p_tenant_id uuid,
            p_engaged boolean
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_tenant_id IS NULL OR p_engaged IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'policy tenant and kill-switch value are required';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.tenants AS tenant WHERE tenant.tenant_id = p_tenant_id
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'policy tenant does not exist';
            END IF;
            INSERT INTO public.tenant_policy_control (tenant_id, kill_switch, updated_at)
            VALUES (p_tenant_id, p_engaged, clock_timestamp())
            ON CONFLICT (tenant_id) DO UPDATE
            SET kill_switch = EXCLUDED.kill_switch,
                updated_at = EXCLUDED.updated_at;
            RETURN p_engaged;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_set_source_policy(
            p_source text,
            p_automation_allowed jsonb,
            p_paid_allowed boolean,
            p_quarantined boolean,
            p_signed_agent_mode text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source NOT IN (
                'meetup', 'luma', 'eventbrite', 'ticketmaster', 'serpapi', 'public_jsonld', 'partiful'
            )
               OR p_automation_allowed IS NULL
               OR NOT public.fn_policy_automation_allowed_is_valid(p_automation_allowed)
               OR p_paid_allowed IS NULL
               OR p_quarantined IS NULL
               OR p_signed_agent_mode NOT IN ('none', 'present-if-honored', 'required')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'invalid source policy control input';
            END IF;
            INSERT INTO public.source_policy (
                source, automation_allowed, paid_allowed, quarantined, signed_agent_mode, updated_at
            )
            VALUES (
                p_source, p_automation_allowed, p_paid_allowed, p_quarantined,
                p_signed_agent_mode, clock_timestamp()
            )
            ON CONFLICT (source) DO UPDATE
            SET automation_allowed = EXCLUDED.automation_allowed,
                paid_allowed = EXCLUDED.paid_allowed,
                quarantined = EXCLUDED.quarantined,
                signed_agent_mode = EXCLUDED.signed_agent_mode,
                updated_at = EXCLUDED.updated_at;
            RETURN true;
        END;
        $$
        """
    )
    for signature, name in (
        (_GLOBAL_SIGNATURE, "fn_set_policy_global_kill_switch"),
        (_TENANT_SIGNATURE, "fn_set_tenant_policy_kill_switch"),
        (_SOURCE_SIGNATURE, "fn_set_source_policy"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}{signature} FROM PUBLIC")


def downgrade() -> None:
    """Remove only the P7a control-plane schema and restore the prior source-policy shape."""
    for table in ("source_policy", "policy_global_control", "tenant_policy_control"):
        op.execute(
            f"DROP TRIGGER IF EXISTS {table}_notify_policy_control_changed ON public.{table}"
        )
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_set_source_policy{_SOURCE_SIGNATURE}")
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_set_tenant_policy_kill_switch{_TENANT_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_set_policy_global_kill_switch{_GLOBAL_SIGNATURE}"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_notify_policy_control_changed()")
    op.execute("DROP TABLE IF EXISTS public.tenant_policy_control")
    op.execute("DROP TABLE IF EXISTS public.policy_global_control")
    op.execute(
        "ALTER TABLE public.source_policy "
        "DROP CONSTRAINT IF EXISTS source_policy_signed_agent_mode_valid"
    )
    op.execute(
        "ALTER TABLE public.source_policy "
        "DROP CONSTRAINT IF EXISTS source_policy_automation_allowed_valid"
    )
    op.execute("ALTER TABLE public.source_policy DROP COLUMN IF EXISTS updated_at")
    op.execute("DROP FUNCTION IF EXISTS public.fn_policy_automation_allowed_is_valid(jsonb)")
