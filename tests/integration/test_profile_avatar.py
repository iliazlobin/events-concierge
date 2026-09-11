"""Avatar removal compares the purged version before deleting a tenant-scoped index row."""

from dataclasses import replace
from uuid import uuid4

import pytest

from events_concierge.adapters.postgres.profile_avatar import PostgresProfileAvatarRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.ports.profile_avatar import ProfileAvatar

pytestmark = pytest.mark.integration


async def test_avatar_compare_delete_preserves_replacements_and_other_tenants(db: None) -> None:
    owner, other = uuid4(), uuid4()
    tenants = PostgresTenantRepository()
    await tenants.add(Tenant(owner, f"avatar-{owner}", "owner@example.test", "owner@u.test"))
    await tenants.add(Tenant(other, f"avatar-{other}", "other@example.test", "other@u.test"))
    avatars = PostgresProfileAvatarRepository()
    avatar = ProfileAvatar("a" * 64 + ".webp", "image/webp", 100, 256, 256, "a" * 64)
    first = await avatars.replace(owner, avatar)
    other_record = await avatars.replace(other, avatar)
    assert await avatars.delete(owner, expected=other_record) is None
    # Re-uploading identical bytes still creates a new index version; an old DELETE must preserve it.
    second = await avatars.replace(owner, avatar)
    assert first.created_at != second.created_at
    assert await avatars.delete(owner, expected=first) is None
    assert await avatars.get(owner) == second
    third = await avatars.replace(
        owner, replace(avatar, storage_key="b" * 64 + ".webp", checksum_sha256="b" * 64)
    )
    assert await avatars.delete(owner, expected=second) is None
    assert await avatars.get(owner) == third
    assert await avatars.delete(owner, expected=third) == third
    assert await avatars.get(owner) is None
    assert await avatars.get(other) == other_record
    assert await avatars.delete(other) == other_record
