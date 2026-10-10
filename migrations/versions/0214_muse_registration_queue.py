"""Registration queue request aliases and versioned in-app progress notifications."""

from collections.abc import Sequence

from alembic import op

revision: str = "0214"
down_revision: str | None = "0213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.muse_signup_items
            ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
            ADD COLUMN seen_version bigint NOT NULL DEFAULT 1
                CHECK (seen_version >= 1 AND seen_version <= version);
        CREATE TABLE public.muse_signup_requests (
            tenant_id uuid NOT NULL REFERENCES public.tenants ON DELETE CASCADE,
            request_id uuid NOT NULL,
            canonical_event_id uuid NOT NULL,
            PRIMARY KEY (tenant_id, request_id),
            FOREIGN KEY (tenant_id, canonical_event_id)
                REFERENCES public.muse_signup_items (tenant_id, canonical_event_id) ON DELETE CASCADE
        );
        ALTER TABLE public.muse_signup_requests ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.muse_signup_requests FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.muse_signup_requests
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        REVOKE ALL ON public.muse_signup_requests FROM PUBLIC;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.muse_signup_requests TO ec_app;
        CREATE TRIGGER tr_account_erasure_write_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.muse_signup_requests
            FOR EACH ROW EXECUTE FUNCTION public.fn_fence_account_erasure_write();
        DROP INDEX public.muse_signup_batches_recency;
        CREATE INDEX muse_signup_batches_recency
            ON public.muse_signup_batches (tenant_id, created_at DESC, batch_id DESC);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE public.muse_signup_requests;
        ALTER TABLE public.muse_signup_items DROP COLUMN seen_version, DROP COLUMN version;
        DROP INDEX public.muse_signup_batches_recency;
        CREATE INDEX muse_signup_batches_recency
            ON public.muse_signup_batches (tenant_id, created_at DESC);
    """)
