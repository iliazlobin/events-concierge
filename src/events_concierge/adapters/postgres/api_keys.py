"""PostgreSQL API key index."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.api_keys import ApiKeyRecord

_COLUMNS = "key_id, name, key_prefix, created_at, last_used_at, revoked_at"


def _record(row: object) -> ApiKeyRecord:
    mapping = dict(row)  # type: ignore[call-overload]
    return ApiKeyRecord(
        key_id=mapping["key_id"],
        name=mapping["name"],
        key_prefix=mapping["key_prefix"],
        created_at=mapping["created_at"],
        last_used_at=mapping["last_used_at"],
        revoked_at=mapping["revoked_at"],
    )


class PostgresApiKeyRepository:
    """Persist API key digests under the owning tenant's FORCE-RLS context."""

    async def list_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """Return every key the tenant owns, newest first.

        ``key_hash`` is deliberately not among the selected columns: nothing above persistence has
        a reason to hold digest material, so it never leaves this module.
        """
        async with tenant_session_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {_COLUMNS}
                                FROM public.tenant_api_keys
                                WHERE tenant_id = :tenant_id
                                ORDER BY created_at DESC"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_record(row) for row in rows)

    async def issue(
        self, tenant_id: UUID, key_id: UUID, name: str, key_prefix: str, key_hash: str
    ) -> ApiKeyRecord:
        """Insert one key digest and return its non-secret record."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""INSERT INTO public.tenant_api_keys (
                                    key_id, tenant_id, name, key_prefix, key_hash
                                )
                                VALUES (:key_id, :tenant_id, :name, :key_prefix, :key_hash)
                                RETURNING {_COLUMNS}"""
                        ),
                        {
                            "key_id": key_id,
                            "tenant_id": tenant_id,
                            "name": name,
                            "key_prefix": key_prefix,
                            "key_hash": key_hash,
                        },
                    )
                )
                .mappings()
                .one()
            )
        return _record(row)

    async def revoke(self, tenant_id: UUID, key_id: UUID) -> ApiKeyRecord | None:
        """Stamp a revocation time, leaving the row in place for attribution.

        The ``revoked_at IS NULL`` guard makes a repeated revoke converge instead of resetting the
        original timestamp, so the record of when a key stopped working stays true.
        """
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""UPDATE public.tenant_api_keys
                                SET revoked_at = clock_timestamp()
                                WHERE tenant_id = :tenant_id
                                  AND key_id = :key_id
                                  AND revoked_at IS NULL
                                RETURNING {_COLUMNS}"""
                        ),
                        {"tenant_id": tenant_id, "key_id": key_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                return _record(row)

            # Either the key never existed for this tenant, or it is already revoked. Report the
            # existing record when there is one so a repeated request is idempotent, not a 404.
            existing = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {_COLUMNS} FROM public.tenant_api_keys
                                WHERE tenant_id = :tenant_id AND key_id = :key_id"""
                        ),
                        {"tenant_id": tenant_id, "key_id": key_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if existing is None else _record(existing)
