"""Index tenant avatar objects held in the configured media store.

Revision ID: 0160
Revises: 0159
Create Date: 2026-08-26

The bytes live in a media store behind ``MediaStorePort`` -- a local filesystem today, blob storage
later -- and this table is the durable index over them.  Splitting the two keeps the store swappable
without a schema change, and keeps the row small enough that the hot ``/v1/me`` read never touches
image data.

Every column here can only be written by a server that actually decoded the upload: ``width_px``,
``height_px``, and ``checksum_sha256`` are outputs of the re-encode, not values a client supplies.
The ``content_type`` vocabulary is closed to the single format the re-encoder emits, so a stored
object can never be served as a type the pipeline did not produce -- notably never ``image/svg+xml``,
which under a same-origin ``img-src`` would be stored XSS against the origin holding the CSRF cookie.

Erasure has two halves: this row disappears with the tenant by cascade, and the object itself is
removed by the media-store purge in the erasure worker's object-store stage (FR-10.5).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0160"
down_revision: str | None = "0159"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# The proxy and the ASGI body guard both cap a request at 64 KiB; holding the stored object well
# under that keeps the upload path inside limits neither layer has to be relaxed to accommodate.
_MAX_AVATAR_BYTES = 32 * 1024


def upgrade() -> None:
    """Create the FORCE-RLS avatar index."""
    op.execute(
        f"""
        CREATE TABLE public.tenant_profile_avatars (
            tenant_id        uuid PRIMARY KEY
                             REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            storage_key      text NOT NULL,
            content_type     text NOT NULL
                             CHECK (content_type = 'image/webp'),
            byte_size        integer NOT NULL
                             CHECK (byte_size > 0 AND byte_size <= {_MAX_AVATAR_BYTES}),
            width_px         integer NOT NULL CHECK (width_px BETWEEN 1 AND 512),
            height_px        integer NOT NULL CHECK (height_px BETWEEN 1 AND 512),
            checksum_sha256  text NOT NULL
                             CHECK (checksum_sha256 ~ '^[0-9a-f]{{64}}$'),
            created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT tenant_profile_avatars_storage_key_opaque CHECK (
                storage_key ~ '^[0-9a-f]{{64}}\\.webp$'
            )
        )
        """
    )
    op.execute("ALTER TABLE public.tenant_profile_avatars ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_profile_avatars FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_profile_avatars_tenant_isolation
        ON public.tenant_profile_avatars
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.tenant_profile_avatars FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tenant_profile_avatars TO ec_app"
    )


def downgrade() -> None:
    """Drop the avatar index and its policy."""
    op.execute(
        "DROP POLICY IF EXISTS tenant_profile_avatars_tenant_isolation "
        "ON public.tenant_profile_avatars"
    )
    op.execute("DROP TABLE IF EXISTS public.tenant_profile_avatars")
