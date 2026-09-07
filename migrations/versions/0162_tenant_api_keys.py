"""Store tenant-issued API keys as digests with a displayable prefix.

Revision ID: 0162
Revises: 0161
Create Date: 2026-08-26

Only a SHA-256 digest of the secret is stored, so the table is not a credential store: a database
disclosure yields no usable key.  ``key_prefix`` holds the short, non-secret leading segment the UI
shows so a person can tell two keys apart without the product ever retaining the secret.  The
plaintext exists once, in the create response, and is never recoverable afterwards.

The digest is a unique index, which is what makes authentication a single indexed lookup rather than
a scan, and simultaneously makes an accidental duplicate impossible.

Revocation is a timestamp rather than a delete so an operator can still answer "what did this key
do" after it stops working; the authenticating query filters on it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0162"
down_revision: str | None = "0161"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create the FORCE-RLS API key index."""
    op.execute(
        """
        CREATE TABLE public.tenant_api_keys (
            key_id        uuid PRIMARY KEY,
            tenant_id     uuid NOT NULL
                          REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            name          text NOT NULL CHECK (
                              name = btrim(name)
                              AND char_length(name) BETWEEN 1 AND 64
                              AND name !~ '[[:cntrl:]]'
                          ),
            key_prefix    text NOT NULL CHECK (key_prefix ~ '^ec_[0-9a-z]{8}$'),
            key_hash      text NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
            created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_used_at  timestamptz,
            revoked_at    timestamptz
        )
        """
    )
    # Authentication resolves a presented secret by digest; this index is that lookup, and its
    # uniqueness also forecloses two accounts ever sharing one key.
    op.execute(
        "CREATE UNIQUE INDEX tenant_api_keys_key_hash_key ON public.tenant_api_keys (key_hash)"
    )
    op.execute(
        """
        CREATE INDEX tenant_api_keys_tenant_created_idx
        ON public.tenant_api_keys (tenant_id, created_at DESC)
        """
    )
    op.execute("ALTER TABLE public.tenant_api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_api_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_api_keys_tenant_isolation
        ON public.tenant_api_keys
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.tenant_api_keys FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tenant_api_keys TO ec_app"
    )


def downgrade() -> None:
    """Drop the API key index and its policy."""
    op.execute("DROP POLICY IF EXISTS tenant_api_keys_tenant_isolation ON public.tenant_api_keys")
    op.execute("DROP TABLE IF EXISTS public.tenant_api_keys")
