"""Durable tenant ranking-profile guarantees (FR-1.2--1.4, FR-2.1, FR-4.3, ADR-001).

The profile store holds only already-aggregated affinity inputs.  These checks exercise its app-role
RLS boundary and replay-safe replacement semantics against real PostgreSQL; they do not invent the
deferred feedback/onboarding product behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from events_concierge.adapters.postgres.ranking import PostgresRankingProfileRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.adapters.ranking.ranker import (
    DeterministicCrossEncoder,
    DeterministicFeatureRescorer,
    PersonalizedRanker,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.events import CanonicalEvent
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.ranking import (
    RankingProfileUpdate,
    RankingProfileUpdateStatus,
    UserRankingProfile,
)

pytestmark = pytest.mark.integration


async def test_ranking_profiles_obey_forced_rls_for_owner_other_unset_and_empty_context(
    db: None,
) -> None:
    """Only the established tenant sees its profile; missing and blank contexts fail closed."""
    owner = await _add_tenant("ranking-profile-owner")
    other = await _add_tenant("ranking-profile-other")
    profiles = PostgresRankingProfileRepository()
    owner_profile = UserRankingProfile(
        explicit_affinities={"jazz": 0.75},
        implicit_affinities={"late night": 0.25},
    )
    other_profile = UserRankingProfile(
        explicit_affinities={"hiking": 0.9},
        implicit_affinities={"outdoors": 0.4},
    )

    owner_write = await profiles.replace_profile(
        owner,
        RankingProfileUpdate(profile=owner_profile, revision=1),
    )
    other_write = await profiles.replace_profile(
        other,
        RankingProfileUpdate(profile=other_profile, revision=1),
    )
    visible = text(
        """SELECT tenant_id, explicit_affinities, implicit_affinities, revision
           FROM public.tenant_ranking_profiles
           ORDER BY tenant_id"""
    )

    async with tenant_session_scope(owner) as session:
        owner_rows = (await session.execute(visible)).mappings().all()
    async with tenant_session_scope(other) as session:
        other_rows = (await session.execute(visible)).mappings().all()
    async with tenant_session_scope(None) as session:
        unset_rows = (await session.execute(visible)).mappings().all()
    async with tenant_session_scope(None) as session:
        empty_context = (
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        ).scalar_one()
        empty_rows = (await session.execute(visible)).mappings().all()

    assert owner_write.status is RankingProfileUpdateStatus.APPLIED
    assert other_write.status is RankingProfileUpdateStatus.APPLIED
    assert _rows(owner_rows) == [(owner, {"jazz": 0.75}, {"late night": 0.25}, 1)]
    assert _rows(other_rows) == [(other, {"hiking": 0.9}, {"outdoors": 0.4}, 1)]
    assert unset_rows == []
    assert empty_context == ""
    assert empty_rows == []


@pytest.mark.parametrize(
    ("explicit_affinities", "implicit_affinities"),
    [
        ("[]", "{}"),
        ('{"   ": 0.5}', "{}"),
        ('{"music": true}', "{}"),
    ],
)
async def test_app_role_rejects_malformed_ranking_profile_maps(
    db: None,
    explicit_affinities: str,
    implicit_affinities: str,
) -> None:
    """The app-role table boundary rejects non-map, blank-label, and boolean-weight JSONB."""
    tenant_id = await _add_tenant("ranking-profile-malformed")
    profiles = PostgresRankingProfileRepository()

    with pytest.raises(Exception, match="tenant_ranking_profiles"):
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO public.tenant_ranking_profiles (
                           tenant_id,
                           explicit_affinities,
                           implicit_affinities,
                           revision
                       )
                       VALUES (
                           :tenant_id,
                           CAST(:explicit_affinities AS jsonb),
                           CAST(:implicit_affinities AS jsonb),
                           1
                       )"""
                ),
                {
                    "tenant_id": tenant_id,
                    "explicit_affinities": explicit_affinities,
                    "implicit_affinities": implicit_affinities,
                },
            )

    assert await profiles.get_profile(tenant_id) == UserRankingProfile()


async def test_ranking_profile_replacement_is_replay_safe_and_stale_writes_do_not_win(
    db: None,
) -> None:
    """An at-least-once retry cannot overwrite a newer tenant profile (FR-2.1, ADR-001)."""
    tenant_id = await _add_tenant("ranking-profile-replay")
    profiles = PostgresRankingProfileRepository()
    first_profile = UserRankingProfile(
        explicit_affinities={"music": 0.4},
        implicit_affinities={"weekday": 0.2},
    )
    current_profile = UserRankingProfile(
        explicit_affinities={"music": 0.8, "comedy": 0.6},
        implicit_affinities={"weekday": 0.1, "downtown": 0.9},
    )

    first = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=first_profile, revision=1),
    )
    replay = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=first_profile, revision=1),
    )
    current = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=current_profile, revision=2),
    )
    stale = await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=first_profile, revision=1),
    )

    with pytest.raises(ValueError):
        await profiles.replace_profile(
            tenant_id,
            RankingProfileUpdate(
                profile=UserRankingProfile(explicit_affinities={"film": 1.0}),
                revision=2,
            ),
        )

    assert first.status is RankingProfileUpdateStatus.APPLIED
    assert first.revision == 1
    assert replay.status is RankingProfileUpdateStatus.REPLAYED
    assert replay.revision == 1
    assert current.status is RankingProfileUpdateStatus.APPLIED
    assert current.revision == 2
    assert stale.status is RankingProfileUpdateStatus.STALE
    assert stale.revision == 2
    assert await profiles.get_profile(tenant_id) == current_profile


async def test_persisted_profile_reorders_the_same_candidate_set(db: None) -> None:
    """Stored declared taste reaches the FR-4.3 re-score rather than stopping at JSON persistence."""
    tenant_id = await _add_tenant("ranking-profile-rerank")
    profiles = PostgresRankingProfileRepository()
    await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(
            profile=UserRankingProfile(explicit_affinities={"jazz": 1.0}),
            revision=1,
        ),
    )
    ranker = PersonalizedRanker(
        embedding=DeterministicEmbedding(dim=16),
        cross_encoder=DeterministicCrossEncoder(),
        profiles=profiles,
        rescorer=DeterministicFeatureRescorer(),
    )
    request = EventRequest(
        request_id=uuid4(),
        tenant_id=tenant_id,
        raw_text="anything",
        constraints=RequestConstraints(),
        intent_embedding=[0.0] * 16,
    )
    python = _candidate("Python meetup", "backend systems")
    jazz = _candidate("Jazz concert", "live music")

    ranked = await ranker.rerank(request, [python, jazz])

    assert ranked[0][0].canonical_event_id == jazz.canonical_event_id


async def test_app_role_cannot_delete_a_tenant_ranking_profile(db: None) -> None:
    """The runtime role may replace its RLS-scoped profile but cannot erase it (FR-2.1)."""
    tenant_id = await _add_tenant("ranking-profile-delete")
    profiles = PostgresRankingProfileRepository()
    profile = UserRankingProfile(explicit_affinities={"design": 0.7})
    await profiles.replace_profile(
        tenant_id,
        RankingProfileUpdate(profile=profile, revision=1),
    )

    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """DELETE FROM public.tenant_ranking_profiles
                       WHERE tenant_id = :tenant_id"""
                ),
                {"tenant_id": tenant_id},
            )

    assert await profiles.get_profile(tenant_id) == profile


async def _add_tenant(prefix: str) -> UUID:
    """Create a real tenant for the profile foreign key and app-role RLS context."""
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, f"oidc|{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    return tenant_id


def _candidate(title: str, description: str) -> CanonicalEvent:
    """Build a minimal candidate whose declared affinity token is visible to the ranker."""
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title=title,
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        description=description,
    )


def _rows(
    rows: Sequence[RowMapping],
) -> list[tuple[UUID, dict[str, float], dict[str, float], int]]:
    """Normalize SQLAlchemy mapping rows for precise RLS assertions without owner bypass."""
    normalized: list[tuple[UUID, dict[str, float], dict[str, float], int]] = []
    for row in rows:
        tenant_id = row["tenant_id"]
        explicit_affinities = row["explicit_affinities"]
        implicit_affinities = row["implicit_affinities"]
        revision = row["revision"]
        if (
            not isinstance(tenant_id, UUID)
            or not isinstance(explicit_affinities, dict)
            or not isinstance(implicit_affinities, dict)
            or not isinstance(revision, int)
        ):
            raise RuntimeError("ranking-profile query returned an unexpected row shape")
        normalized.append(
            (
                tenant_id,
                _affinities(explicit_affinities),
                _affinities(implicit_affinities),
                revision,
            )
        )
    return normalized


def _affinities(value: dict[object, object]) -> dict[str, float]:
    """Assert the JSONB map is a valid profile-map shape before comparing its values."""
    result: dict[str, float] = {}
    for label, weight in value.items():
        if (
            not isinstance(label, str)
            or isinstance(weight, bool)
            or not isinstance(weight, int | float)
        ):
            raise RuntimeError("ranking-profile JSONB map returned an unexpected value")
        result[label] = float(weight)
    return result
