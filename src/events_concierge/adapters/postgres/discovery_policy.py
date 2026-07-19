"""Fresh PostgreSQL reader for tenant-neutral discovery dispatch policy (FR-3.9/FR-10.1)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ...domain.enums import Modality, Source
from ...domain.policy import SourcePolicy
from ...infra.db import system_session_scope


class PostgresDiscoveryPolicyReader:
    """Read one durable source-policy row under the non-superuser application role.

    `source_policy` is tenant-neutral and has no RLS policy.  The application role receives only
    SELECT, and a query failure intentionally propagates to the application-layer gate, which
    denies the dispatch rather than relying on a cached/default allow (FR-3.9/FR-10.1, AC-23).
    """

    async def read_source_policy(self, source: Source) -> SourcePolicy | None:
        """Load and validate the exact current policy row for one source."""
        async with system_session_scope() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT source,
                                      automation_allowed,
                                      paid_allowed,
                                      quarantined,
                                      signed_agent_mode
                               FROM public.source_policy
                               WHERE source = :source"""
                        ),
                        {"source": source.value},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _source_policy_from_row(row, source)


def _source_policy_from_row(row: RowMapping, expected_source: Source) -> SourcePolicy:
    """Lift a validated source-policy row into the closed domain shape."""
    source = row["source"]
    if source != expected_source.value:
        raise RuntimeError("policy source row did not match the requested source")
    raw_allowed = row["automation_allowed"]
    paid_allowed = row["paid_allowed"]
    quarantined = row["quarantined"]
    signed_agent_mode = row["signed_agent_mode"]
    if not isinstance(raw_allowed, dict):
        raise RuntimeError("policy automation_allowed is not a JSON object")
    if not isinstance(paid_allowed, bool) or not isinstance(quarantined, bool):
        raise RuntimeError("policy source booleans are malformed")
    if signed_agent_mode not in {"none", "present-if-honored", "required"}:
        raise RuntimeError("policy signed-agent mode is malformed")

    automation_allowed: dict[Modality, bool] = {}
    for raw_modality, enabled in cast(dict[object, object], raw_allowed).items():
        if not isinstance(raw_modality, str) or not isinstance(enabled, bool):
            raise RuntimeError("policy automation_allowed contains a malformed entry")
        try:
            modality = Modality(raw_modality)
        except ValueError as error:
            raise RuntimeError("policy automation_allowed contains an unknown modality") from error
        automation_allowed[modality] = enabled
    return SourcePolicy(
        source=expected_source,
        automation_allowed=automation_allowed,
        paid_allowed=paid_allowed,
        quarantined=quarantined,
        signed_agent_mode=signed_agent_mode,
    )
