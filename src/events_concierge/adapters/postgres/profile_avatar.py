"""PostgreSQL avatar index.

Holds only the pointer and the decode-derived facts; the bytes live behind ``MediaStorePort``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.profile_avatar import ProfileAvatar

_COLUMNS = "storage_key, content_type, byte_size, width_px, height_px, checksum_sha256, created_at"


def _record(row: object) -> ProfileAvatar:
    mapping = dict(row)  # type: ignore[call-overload]
    return ProfileAvatar(
        storage_key=mapping["storage_key"],
        content_type=mapping["content_type"],
        byte_size=int(mapping["byte_size"]),
        width_px=int(mapping["width_px"]),
        height_px=int(mapping["height_px"]),
        checksum_sha256=mapping["checksum_sha256"],
        created_at=mapping["created_at"],
    )


class PostgresProfileAvatarRepository:
    """Persist one avatar pointer per tenant under its FORCE-RLS context."""

    async def get(self, tenant_id: UUID) -> ProfileAvatar | None:
        """Return the current record, or ``None`` when the tenant has no avatar."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {_COLUMNS}
                                FROM public.tenant_profile_avatars
                                WHERE tenant_id = :tenant_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _record(row)

    async def replace(self, tenant_id: UUID, avatar: ProfileAvatar) -> ProfileAvatar:
        """Upsert the pointer unconditionally.

        A whole-value replace needs no revision: the object is content addressed, so re-uploading
        identical bytes converges on the same row rather than racing anything.
        """
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""INSERT INTO public.tenant_profile_avatars (
                                    tenant_id, storage_key, content_type, byte_size,
                                    width_px, height_px, checksum_sha256
                                )
                                VALUES (
                                    :tenant_id, :storage_key, :content_type, :byte_size,
                                    :width_px, :height_px, :checksum_sha256
                                )
                                ON CONFLICT (tenant_id) DO UPDATE
                                SET storage_key = EXCLUDED.storage_key,
                                    content_type = EXCLUDED.content_type,
                                    byte_size = EXCLUDED.byte_size,
                                    width_px = EXCLUDED.width_px,
                                    height_px = EXCLUDED.height_px,
                                    checksum_sha256 = EXCLUDED.checksum_sha256,
                                    created_at = clock_timestamp()
                                RETURNING {_COLUMNS}"""
                        ),
                        {
                            "tenant_id": tenant_id,
                            "storage_key": avatar.storage_key,
                            "content_type": avatar.content_type,
                            "byte_size": avatar.byte_size,
                            "width_px": avatar.width_px,
                            "height_px": avatar.height_px,
                            "checksum_sha256": avatar.checksum_sha256,
                        },
                    )
                )
                .mappings()
                .one()
            )
        return _record(row)

    async def delete(
        self, tenant_id: UUID, *, expected: ProfileAvatar | None = None
    ) -> ProfileAvatar | None:
        """Remove only the expected version when its object has already been purged."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""DELETE FROM public.tenant_profile_avatars
                                WHERE tenant_id = :tenant_id
                                  AND (:unconditional OR (
                                      storage_key = :expected_key
                                      AND created_at IS NOT DISTINCT FROM
                                          CAST(:expected_created_at AS timestamptz)
                                  ))
                                RETURNING {_COLUMNS}"""
                        ),
                        {
                            "tenant_id": tenant_id,
                            "unconditional": expected is None,
                            "expected_key": expected.storage_key if expected else None,
                            "expected_created_at": expected.created_at if expected else None,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _record(row)
