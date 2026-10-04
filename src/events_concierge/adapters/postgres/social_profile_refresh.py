"""Fenced social snapshots; no direct table access for the application role."""

from __future__ import annotations

import json

from sqlalchemy import text

from ...application.social_profile_enrichment import SocialProfileClaim
from ...infra.db import system_session_scope
from ..entity_intelligence.public_sources import CollectedPublicSource


class PostgresSocialProfileRefreshRepository:
    async def claim(
        self, providers: tuple[str, ...], daily_limit: int
    ) -> SocialProfileClaim | None:
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT * FROM public.fn_claim_catalog_social_refresh_v1("
                        "CAST(:providers AS text[]), :daily_limit)"
                    ),
                    {"providers": list(providers), "daily_limit": daily_limit},
                )
            ).first()
        return (
            None
            if row is None
            else SocialProfileClaim(
                row.entity_id, row.provider_key, row.source_url, row.previous_id, row.lease_token
            )
        )

    async def finish(
        self,
        claim: SocialProfileClaim,
        collection: CollectedPublicSource | None,
        *,
        error_code: str | None,
        refresh_seconds: int,
    ) -> bool:
        facts = (
            []
            if collection is None
            else [
                {
                    "key": fact.fact_key,
                    "value": fact.value,
                    "url": fact.value_url,
                    "order": fact.sort_order,
                }
                for fact in collection.facts
            ]
        )
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            "SELECT public.fn_finish_catalog_social_refresh_v1("
                            ":entity_id, :provider_key, :token, :external_id, "
                            "CAST(:facts AS jsonb), :error_code, :refresh_seconds)"
                        ),
                        {
                            "entity_id": claim.entity_id,
                            "provider_key": claim.provider_key,
                            "token": claim.lease_token,
                            "external_id": None if collection is None else collection.external_id,
                            "facts": json.dumps(facts, separators=(",", ":")),
                            "error_code": error_code,
                            "refresh_seconds": refresh_seconds,
                        },
                    )
                ).scalar_one()
            )
