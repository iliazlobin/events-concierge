"""Private executor-account preferences and immutable claimed signup identities."""

from collections.abc import Sequence

from alembic import op

revision: str = "0219"
down_revision: str | None = "0218"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.registration_account_settings (
            tenant_id uuid NOT NULL REFERENCES public.tenants ON DELETE CASCADE,
            executor text NOT NULL CHECK (executor = 'muse'),
            expected_email text CHECK (length(expected_email) BETWEEN 3 AND 254),
            provider_overrides jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(provider_overrides) = 'object'),
            revision bigint NOT NULL CHECK (revision >= 1),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, executor)
        );
        ALTER TABLE public.registration_account_settings ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.registration_account_settings FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.registration_account_settings
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        REVOKE ALL ON public.registration_account_settings FROM PUBLIC;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.registration_account_settings TO ec_app;
        CREATE TRIGGER tr_account_erasure_write_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.registration_account_settings
            FOR EACH ROW EXECUTE FUNCTION public.fn_fence_account_erasure_write();
        ALTER TABLE public.muse_signup_items ADD COLUMN registration_account jsonb
            CHECK (registration_account IS NULL OR jsonb_typeof(registration_account) = 'object');
    """)


def downgrade() -> None:
    op.execute("""
        LOCK TABLE public.registration_account_settings, public.muse_signup_items IN ACCESS EXCLUSIVE MODE;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.registration_account_settings)
                OR EXISTS (SELECT 1 FROM public.muse_signup_items WHERE registration_account IS NOT NULL)
            THEN
                RAISE EXCEPTION 'registration account data is retained; roll back code while keeping migration 0219';
            END IF;
        END $$;
        ALTER TABLE public.muse_signup_items DROP COLUMN registration_account;
        DROP TABLE public.registration_account_settings;
    """)
