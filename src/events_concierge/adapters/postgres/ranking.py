"""PostgreSQL tenant-ranking profile repository (FR-1.2--FR-1.4, FR-2.1, FR-4.3).

Only the current, already-aggregated affinity maps live here. The repository deliberately has no
opinion about feedback collection, merge weights, decay, onboarding, or model training; a caller
supplies one whole profile and a monotonic revision through the narrow port.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ...infra.db import tenant_session_scope
from ...ports.ranking import (
    RankingProfileUpdate,
    RankingProfileUpdateResult,
    RankingProfileUpdateStatus,
    UserRankingProfile,
)


class PostgresRankingProfileRepository:
    """Persist a single tenant's revisioned profile under its FORCE-RLS context.

    Reads with an absent row return the neutral profile required for cold start. Replacement uses
    a conditional upsert: a newer revision changes state once, an exact retry converges as
    ``REPLAYED``, and a stale delivery returns the newer stored state without overwriting it
    (FR-2.1, FR-4.3, FR-4.4, ADR-001).
    """

    async def get_profile(self, tenant_id: UUID) -> UserRankingProfile:
        """Return this tenant's durable profile, or neutral cold-start inputs when absent."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT explicit_affinities, implicit_affinities
                               FROM public.tenant_ranking_profiles
                               WHERE tenant_id = :tenant_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return UserRankingProfile() if row is None else _profile_from_row(row)

    async def replace_profile(
        self, tenant_id: UUID, update: RankingProfileUpdate
    ) -> RankingProfileUpdateResult:
        """Apply an idempotent whole-profile replacement under the tenant RLS boundary."""
        explicit_affinities = _encode_affinities(update.profile.explicit_affinities)
        implicit_affinities = _encode_affinities(update.profile.implicit_affinities)
        parameters = {
            "tenant_id": tenant_id,
            "explicit_affinities": explicit_affinities,
            "implicit_affinities": implicit_affinities,
            "revision": update.revision,
        }
        async with tenant_session_scope(tenant_id) as session:
            applied_row = (
                (
                    await session.execute(
                        text(
                            """INSERT INTO public.tenant_ranking_profiles AS current_profile (
                                   tenant_id,
                                   explicit_affinities,
                                   implicit_affinities,
                                   revision
                               )
                               VALUES (
                                   :tenant_id,
                                   CAST(:explicit_affinities AS jsonb),
                                   CAST(:implicit_affinities AS jsonb),
                                   :revision
                               )
                               ON CONFLICT (tenant_id) DO UPDATE
                               SET explicit_affinities = EXCLUDED.explicit_affinities,
                                   implicit_affinities = EXCLUDED.implicit_affinities,
                                   revision = EXCLUDED.revision,
                                   updated_at = clock_timestamp()
                               WHERE current_profile.revision < EXCLUDED.revision
                               RETURNING explicit_affinities, implicit_affinities, revision"""
                        ),
                        parameters,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if applied_row is not None:
                return _applied_result(applied_row, update)

            stored_row = (
                (
                    await session.execute(
                        text(
                            """SELECT explicit_affinities, implicit_affinities, revision
                               FROM public.tenant_ranking_profiles
                               WHERE tenant_id = :tenant_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if stored_row is None:
                raise RuntimeError("ranking profile disappeared during revisioned replacement")
            stored_profile = _profile_from_row(stored_row)
            stored_revision = _revision_from_row(stored_row)

        if stored_revision == update.revision:
            if stored_profile != update.profile:
                raise ValueError("ranking profile revision is already bound to a different profile")
            return RankingProfileUpdateResult(
                status=RankingProfileUpdateStatus.REPLAYED,
                profile=stored_profile,
                revision=stored_revision,
            )
        if stored_revision > update.revision:
            return RankingProfileUpdateResult(
                status=RankingProfileUpdateStatus.STALE,
                profile=stored_profile,
                revision=stored_revision,
            )
        raise RuntimeError("ranking profile revision did not converge after conditional upsert")


def _applied_result(row: RowMapping, update: RankingProfileUpdate) -> RankingProfileUpdateResult:
    """Validate PostgreSQL's RETURNING row before exposing a successful write."""
    profile = _profile_from_row(row)
    revision = _revision_from_row(row)
    if profile != update.profile or revision != update.revision:
        raise RuntimeError("ranking profile upsert returned state different from its input")
    return RankingProfileUpdateResult(
        status=RankingProfileUpdateStatus.APPLIED,
        profile=profile,
        revision=revision,
    )


def _encode_affinities(affinities: Mapping[str, float]) -> str:
    """Produce deterministic valid JSON for PostgreSQL JSONB equality and test doubles."""
    return json.dumps(
        dict(affinities),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _profile_from_row(row: RowMapping) -> UserRankingProfile:
    """Decode an integrity-checked database row and fail closed if it is malformed."""
    try:
        return UserRankingProfile(
            explicit_affinities=_affinities_from_json(
                row["explicit_affinities"], "explicit_affinities"
            ),
            implicit_affinities=_affinities_from_json(
                row["implicit_affinities"], "implicit_affinities"
            ),
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("tenant ranking profile row is malformed") from error


def _affinities_from_json(value: object, column: str) -> Mapping[str, float]:
    """Accept psycopg's decoded JSONB value or its string fallback, never another JSON shape."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError(f"tenant ranking profile {column} is not a JSON object")
    return cast("Mapping[str, float]", decoded)


def _revision_from_row(row: RowMapping) -> int:
    """Validate the database's monotonic revision before returning it to application code."""
    revision = row["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RuntimeError("tenant ranking profile revision is malformed")
    return revision
