"""Tenant-scoped Muse connections and selected free-event signup batches."""

from collections.abc import Sequence

from alembic import op

revision: str = "0209"
down_revision: str | None = "0208"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("muse_connections", "muse_signup_batches", "muse_signup_items")
_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.muse_connections (
            tenant_id uuid PRIMARY KEY REFERENCES public.tenants ON DELETE CASCADE,
            token_hash text NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz
        );
        CREATE TABLE public.muse_signup_batches (
            batch_id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES public.tenants ON DELETE CASCADE,
            request_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (tenant_id, request_id),
            UNIQUE (tenant_id, batch_id)
        );
        CREATE TABLE public.muse_signup_items (
            tenant_id uuid NOT NULL REFERENCES public.tenants ON DELETE CASCADE,
            batch_id uuid NOT NULL,
            canonical_event_id uuid NOT NULL,
            event jsonb NOT NULL,
            status text NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'in_progress', 'needs_input', 'awaiting_approval',
                'waitlisted', 'registered', 'failed', 'uncertain'
            )),
            attempt_id uuid,
            outcome jsonb,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, canonical_event_id),
            FOREIGN KEY (tenant_id, batch_id)
                REFERENCES public.muse_signup_batches (tenant_id, batch_id) ON DELETE CASCADE,
            CHECK ((status = 'queued') = (attempt_id IS NULL))
        );
        CREATE INDEX muse_signup_batches_recency ON public.muse_signup_batches (tenant_id, created_at DESC);
        CREATE INDEX muse_signup_items_batch ON public.muse_signup_items (tenant_id, batch_id);
    """)
    for table in _TABLES:
        op.execute(f"""
            ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON public.{table}
                USING ({_POLICY}) WITH CHECK ({_POLICY});
            REVOKE ALL ON public.{table} FROM PUBLIC;
            GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO ec_app;
            CREATE TRIGGER tr_account_erasure_write_fence
                BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.fn_fence_account_erasure_write();
        """)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE public.{table}")
