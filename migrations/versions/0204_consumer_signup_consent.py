"""Atomic self-service consumer accounts and immutable, tenant-scoped legal receipts.

Revision ID: 0204
Revises: 0203
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0204"
down_revision: str | None = "0203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(uuid,text,text,text,text,text,text)"


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
                RAISE EXCEPTION '0204 signup requires a BYPASSRLS migration owner';
            END IF;
        END $$;
        CREATE TABLE public.consumer_account_consents (
            tenant_id uuid NOT NULL REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            terms_version text NOT NULL CHECK (length(terms_version) BETWEEN 1 AND 80),
            terms_url text NOT NULL CHECK (length(terms_url) BETWEEN 1 AND 2048),
            privacy_version text NOT NULL CHECK (length(privacy_version) BETWEEN 1 AND 80),
            privacy_url text NOT NULL CHECK (length(privacy_url) BETWEEN 1 AND 2048),
            accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, terms_version, privacy_version)
        );
        ALTER TABLE public.consumer_account_consents ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.consumer_account_consents FORCE ROW LEVEL SECURITY;
        CREATE POLICY consumer_account_consents_tenant ON public.consumer_account_consents
            USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
        REVOKE ALL ON public.consumer_account_consents FROM PUBLIC, ec_app;
        GRANT SELECT ON public.consumer_account_consents TO ec_app;
        CREATE TRIGGER tr_account_erasure_write_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.consumer_account_consents
            FOR EACH ROW EXECUTE FUNCTION public.fn_fence_account_erasure_write();

        CREATE FUNCTION public.fn_accept_consumer_account(
            p_tenant_id uuid, p_subject text, p_email text,
            p_terms_version text, p_terms_url text, p_privacy_version text, p_privacy_url text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            v_hash bytea;
            v_expected_id uuid;
            v_existing_subject text;
        BEGIN
            IF p_tenant_id IS NULL OR p_subject IS NULL
               OR p_subject COLLATE "C" !~ '^identity-platform:v1:[a-z][a-z0-9-]{4,61}[a-z0-9]:[!-~]{1,128}$'
               OR p_email IS NULL OR length(p_email) NOT BETWEEN 3 AND 320
               OR p_email !~ '^[^[:space:]@]+@[^[:space:]@]+$'
               OR p_terms_version IS NULL OR p_terms_version !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$'
               OR p_privacy_version IS NULL OR p_privacy_version !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$'
               OR p_terms_url IS NULL OR length(p_terms_url) > 2048 OR p_terms_url !~ '^https://'
               OR p_privacy_url IS NULL OR length(p_privacy_url) > 2048 OR p_privacy_url !~ '^https://'
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid consumer signup';
            END IF;
            -- UUIDv8(SHA-256, immutable subject): the database enforces the same mapping as
            -- the API. Erasure cannot be bypassed by retrying this subject under a random UUID.
            v_hash := sha256(convert_to('https://events-concierge/accounts/v1/' || p_subject,'UTF8'));
            v_hash := set_byte(v_hash,6,(get_byte(v_hash,6) & 15) | 128);
            v_hash := set_byte(v_hash,8,(get_byte(v_hash,8) & 63) | 128);
            v_expected_id := encode(substring(v_hash FROM 1 FOR 16),'hex')::uuid;
            IF p_tenant_id <> v_expected_id THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid consumer account binding';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended('account-erasure:' || p_tenant_id::text,0));
            IF EXISTS (SELECT 1 FROM public.account_erasure_requests WHERE tenant_id = p_tenant_id) THEN
                RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'consumer account unavailable';
            END IF;
            SELECT oidc_subject INTO v_existing_subject FROM public.tenants WHERE tenant_id = p_tenant_id;
            IF FOUND AND v_existing_subject <> p_subject THEN
                RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'consumer account unavailable';
            END IF;
            IF NOT FOUND THEN
                INSERT INTO public.tenants(tenant_id,oidc_subject,notify_email,relay_inbox)
                VALUES (p_tenant_id,p_subject,p_email,p_tenant_id::text || '@accounts.invalid');
            END IF;
            INSERT INTO public.consumer_account_consents
                (tenant_id,terms_version,terms_url,privacy_version,privacy_url)
            VALUES (p_tenant_id,p_terms_version,p_terms_url,p_privacy_version,p_privacy_url)
            ON CONFLICT DO NOTHING;
            IF NOT EXISTS (SELECT 1 FROM public.consumer_account_consents
                WHERE tenant_id = p_tenant_id AND terms_version = p_terms_version
                AND privacy_version = p_privacy_version AND terms_url = p_terms_url
                AND privacy_url = p_privacy_url) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'legal document version changed';
            END IF;
            RETURN p_tenant_id;
        END $$;
        REVOKE ALL ON FUNCTION public.fn_accept_consumer_account(uuid,text,text,text,text,text,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.fn_accept_consumer_account(uuid,text,text,text,text,text,text) TO ec_app;
    """)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION public.fn_accept_consumer_account{_SIGNATURE}")
    op.execute("DROP TABLE public.consumer_account_consents")
