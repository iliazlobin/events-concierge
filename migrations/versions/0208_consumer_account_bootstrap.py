"""Account binding for explicitly deferred legal acceptance; no consent receipts.

Revision ID: 0208
Revises: 0207
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0208"
down_revision: str | None = "0207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = current_user AND (rolsuper OR rolbypassrls)) THEN
                RAISE EXCEPTION '0208 signup requires a BYPASSRLS migration owner';
            END IF;
        END $$;
        CREATE FUNCTION public.fn_bootstrap_consumer_account(
            p_tenant_id uuid, p_subject text, p_email text
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
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid consumer signup';
            END IF;
            -- Match the API's UUIDv8 binding and serialize with retained erasure fences.
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
            IF FOUND AND v_existing_subject IS DISTINCT FROM p_subject THEN
                RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'consumer account unavailable';
            END IF;
            IF NOT FOUND THEN
                INSERT INTO public.tenants(tenant_id,oidc_subject,notify_email,relay_inbox)
                VALUES (p_tenant_id,p_subject,p_email,p_tenant_id::text || '@accounts.invalid');
            END IF;
            RETURN p_tenant_id;
        END $$;
        REVOKE ALL ON FUNCTION public.fn_bootstrap_consumer_account(uuid,text,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.fn_bootstrap_consumer_account(uuid,text,text) TO ec_app;
    """)


def downgrade() -> None:
    # Preserve accounts and all genuine legal receipts when reverting the capability.
    op.execute("DROP FUNCTION public.fn_bootstrap_consumer_account(uuid,text,text)")
