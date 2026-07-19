"""Unit contracts for durable-compatible per-tenant ranking profiles (FR-2.1/FR-4.3/FR-4.4).

The in-memory adapter deliberately implements the same revision/idempotency semantics as the
durable repository, so retry behaviour remains testable without PostgreSQL.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import cast
from uuid import uuid4

import pytest

from events_concierge.adapters.ranking.ranker import InMemoryRankingProfiles
from events_concierge.ports.ranking import (
    RankingProfileUpdate,
    RankingProfileUpdateStatus,
    UserRankingProfile,
)


def test_user_ranking_profile_normalizes_affinities_to_immutable_finite_maps() -> None:
    """Profile equality and durable JSON encoding cannot depend on caller-owned mutable input."""
    profile = UserRankingProfile(
        explicit_affinities={"  jazz  ": 2, "python": -0.25},
        implicit_affinities={"  waterfront ": 0.5},
    )

    assert dict(profile.explicit_affinities) == {"jazz": 2.0, "python": -0.25}
    assert dict(profile.implicit_affinities) == {"waterfront": 0.5}
    with pytest.raises(TypeError):
        cast(MutableMapping[str, float], profile.explicit_affinities)["new-label"] = 1.0


@pytest.mark.parametrize(
    "affinities",
    [
        {"": 1.0},
        {"   ": 1.0},
        {1: 1.0},
        {"jazz": True},
        {"jazz": float("nan")},
        {"jazz": float("inf")},
        {"jazz": float("-inf")},
        {"jazz": "1.0"},
    ],
)
def test_user_ranking_profile_rejects_invalid_affinities(
    affinities: dict[object, object],
) -> None:
    """Only nonblank labels with finite, non-boolean numeric weights reach the ranker."""
    with pytest.raises((TypeError, ValueError)):
        UserRankingProfile(explicit_affinities=cast(dict[str, float], affinities))


@pytest.mark.parametrize("revision", [0, -1, True])
def test_ranking_profile_update_requires_a_positive_integer_revision(revision: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        RankingProfileUpdate(profile=UserRankingProfile(), revision=revision)


async def test_in_memory_ranking_profiles_returns_a_neutral_profile_when_absent() -> None:
    profiles = InMemoryRankingProfiles()

    assert await profiles.get_profile(uuid4()) == UserRankingProfile()


async def test_in_memory_ranking_profiles_applies_new_and_higher_revisions() -> None:
    tenant_id = uuid4()
    profiles = InMemoryRankingProfiles()
    initial = UserRankingProfile(explicit_affinities={"jazz": 1.0})
    replacement = UserRankingProfile(implicit_affinities={"python": 0.75})

    first = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=initial, revision=1),
    )
    second = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=replacement, revision=2),
    )

    assert first.status is RankingProfileUpdateStatus.APPLIED
    assert first.revision == 1
    assert second.status is RankingProfileUpdateStatus.APPLIED
    assert second.revision == 2
    assert await profiles.get_profile(tenant_id) == replacement


async def test_in_memory_ranking_profiles_replays_an_exact_delivery_at_the_same_revision() -> None:
    tenant_id = uuid4()
    profile = UserRankingProfile(explicit_affinities={"jazz": 1.0})
    profiles = InMemoryRankingProfiles()
    update = RankingProfileUpdate(profile=profile, revision=4)

    applied = await profiles.replace_profile(tenant_id, update)
    replayed = await profiles.replace_profile(tenant_id, update)

    assert applied.status is RankingProfileUpdateStatus.APPLIED
    assert replayed.status is RankingProfileUpdateStatus.REPLAYED
    assert replayed.revision == 4
    assert await profiles.get_profile(tenant_id) == profile


async def test_in_memory_ranking_profiles_rejects_a_conflicting_same_revision() -> None:
    tenant_id = uuid4()
    profiles = InMemoryRankingProfiles()
    await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(
            profile=UserRankingProfile(explicit_affinities={"jazz": 1.0}),
            revision=4,
        ),
    )

    with pytest.raises(ValueError, match="revision"):
        await profiles.replace_profile(
            tenant_id,
            RankingProfileUpdate(
                profile=UserRankingProfile(explicit_affinities={"python": 1.0}),
                revision=4,
            ),
        )


async def test_in_memory_ranking_profiles_marks_lower_revisions_stale_without_overwrite() -> None:
    tenant_id = uuid4()
    current = UserRankingProfile(explicit_affinities={"jazz": 1.0})
    stale_profile = UserRankingProfile(explicit_affinities={"python": 1.0})
    profiles = InMemoryRankingProfiles()
    await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=current, revision=5),
    )

    stale = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=stale_profile, revision=4),
    )

    assert stale.status is RankingProfileUpdateStatus.STALE
    assert stale.revision == 5
    assert await profiles.get_profile(tenant_id) == current


async def test_in_memory_constructor_profiles_start_at_revision_zero_for_fixture_compatibility() -> (
    None
):
    tenant_id = uuid4()
    profile = UserRankingProfile(explicit_affinities={"jazz": 1.0})
    replacement = UserRankingProfile(explicit_affinities={"python": 1.0})
    profiles = InMemoryRankingProfiles({tenant_id: profile})

    applied = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=replacement, revision=1),
    )

    assert applied.status is RankingProfileUpdateStatus.APPLIED
    assert applied.revision == 1
    assert await profiles.get_profile(tenant_id) == replacement
