"""Immutable PostgreSQL feedback receipts and derived implicit-affinity reads.

The receipt stream is the sole durable source for implicit affinities: a retry first attempts one
tenant-scoped insert, and aggregation reads only rows that committed successfully.  This avoids a
separate mutable counter that could drift from an at-least-once feedback delivery (FR-1.3/1.4,
FR-4.3, NFR-8, ADR-001).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from math import isfinite
from numbers import Real
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ...domain.ranking_feedback import (
    RankingFeedbackReceipt,
    bound_implicit_affinity,
    normalize_feedback_feature_deltas,
)
from ...infra.db import tenant_session_scope
from ...ports.ranking_feedback import (
    RankingFeedbackConflictError,
    RankingFeedbackRecordStatus,
)


class PostgresRankingFeedbackRepository:
    """Append/replay tenant feedback and derive its compact implicit-affinity map.

    ``(tenant_id, signal_id)`` is the immutable idempotency boundary.  No aggregate is updated
    during recording: a successful read sums only committed receipt deltas, so a failed insert,
    conflict, or replay cannot fabricate a learned preference (FR-4.3, NFR-8).
    """

    async def record_feedback(
        self, tenant_id: UUID, receipt: RankingFeedbackReceipt
    ) -> RankingFeedbackRecordStatus:
        """Persist one receipt, replay an identical retry, or reject a conflicting reuse."""
        feature_deltas = _normalize_deltas(receipt.feature_deltas, "feature_deltas")
        parameters = {
            "tenant_id": tenant_id,
            "signal_id": receipt.signal.signal_id,
            "canonical_event_id": receipt.signal.canonical_event_id,
            "signal_kind": receipt.signal.kind.value,
            "feature_deltas": _encode_deltas(feature_deltas),
        }
        async with tenant_session_scope(tenant_id) as session:
            inserted = (
                await session.execute(
                    text(
                        """INSERT INTO public.tenant_ranking_feedback_receipts (
                               tenant_id,
                               signal_id,
                               canonical_event_id,
                               signal_kind,
                               feature_deltas
                           )
                           VALUES (
                               :tenant_id,
                               :signal_id,
                               :canonical_event_id,
                               :signal_kind,
                               CAST(:feature_deltas AS jsonb)
                           )
                           ON CONFLICT (tenant_id, signal_id) DO NOTHING
                           RETURNING signal_id"""
                    ),
                    parameters,
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return RankingFeedbackRecordStatus.RECORDED

            existing = (
                (
                    await session.execute(
                        text(
                            """SELECT canonical_event_id, signal_kind, feature_deltas
                               FROM public.tenant_ranking_feedback_receipts
                               WHERE tenant_id = :tenant_id AND signal_id = :signal_id"""
                        ),
                        {"tenant_id": tenant_id, "signal_id": receipt.signal.signal_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if existing is None:
            raise RuntimeError("ranking feedback receipt disappeared after an insert conflict")
        if _is_exact_replay(existing, receipt):
            return RankingFeedbackRecordStatus.REPLAYED
        raise RankingFeedbackConflictError(
            "ranking feedback signal id is already bound to a different payload"
        )

    async def get_implicit_affinities(self, tenant_id: UUID) -> Mapping[str, float]:
        """Sum committed tenant receipt deltas without reading another tenant's behavior."""
        async with tenant_session_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        """SELECT COALESCE(
                                   jsonb_object_agg(
                                       aggregate.label,
                                       LEAST(5::numeric, GREATEST(-5::numeric, aggregate.total))
                                   ),
                                   '{}'::jsonb
                               )
                           FROM (
                               SELECT delta.label,
                                      sum((delta.weight #>> '{}')::numeric) AS total
                               FROM public.tenant_ranking_feedback_receipts AS receipt
                               CROSS JOIN LATERAL jsonb_each(receipt.feature_deltas)
                                   AS delta(label, weight)
                               WHERE receipt.tenant_id = :tenant_id
                               GROUP BY delta.label
                           ) AS aggregate
                           WHERE aggregate.total <> 0"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one()
        return _decode_aggregate(value)


def _is_exact_replay(
    row: RowMapping,
    receipt: RankingFeedbackReceipt,
) -> bool:
    """Compare client-visible signal identity while retaining the first durable feature snapshot.

    Canonical event metadata can be enriched between attempts. Its first derived feature map is
    therefore immutable receipt data, not a replay input; otherwise one at-least-once client retry
    could conflict merely because a public description or venue changed after the original write.
    """
    canonical_event_id = row["canonical_event_id"]
    signal_kind = row["signal_kind"]
    if not isinstance(canonical_event_id, UUID) or not isinstance(signal_kind, str):
        raise RuntimeError("ranking feedback receipt row is malformed")
    # Validate the first-write snapshot even though it is intentionally not compared to a newly
    # derived map. A corrupt receipt must not be treated as a harmless replay.
    _decode_deltas(row["feature_deltas"], "feature_deltas")
    return (
        canonical_event_id == receipt.signal.canonical_event_id
        and signal_kind == receipt.signal.kind.value
    )


def _encode_deltas(feature_deltas: Mapping[str, float]) -> str:
    """Serialize normalized deltas deterministically for JSONB storage and exact replay checks."""
    return json.dumps(
        dict(feature_deltas),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_deltas(value: object, field_name: str) -> Mapping[str, float]:
    """Decode a database JSONB map and retain the port's finite-label invariant."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise RuntimeError(f"ranking feedback {field_name} is not a JSON object")
    try:
        return normalize_feedback_feature_deltas(decoded)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"ranking feedback {field_name} is malformed") from error


def _normalize_deltas(values: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    """Use the domain's bounded token/delta invariant before serializing an insert."""
    try:
        return normalize_feedback_feature_deltas(values)
    except (TypeError, ValueError) as error:
        raise type(error)(f"ranking feedback {field_name} is invalid") from error


def _decode_aggregate(value: object) -> Mapping[str, float]:
    """Decode bounded SQL totals without treating an aggregate as one receipt delta map."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise RuntimeError("ranking feedback implicit affinity aggregate is not a JSON object")
    normalized: dict[str, float] = {}
    for raw_label, raw_weight in decoded.items():
        if not isinstance(raw_label, str):
            raise RuntimeError("ranking feedback aggregate label is malformed")
        # Reuse the receipt validator for label grammar without imposing its <= 1 delta bound on
        # a cumulative total.
        try:
            label = next(iter(normalize_feedback_feature_deltas({raw_label: 1.0})))
        except (TypeError, ValueError) as error:
            raise RuntimeError("ranking feedback aggregate label is malformed") from error
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, Real):
            raise RuntimeError("ranking feedback aggregate weight is malformed")
        total = float(raw_weight)
        if not isfinite(total):
            raise RuntimeError("ranking feedback aggregate weight is non-finite")
        bounded = bound_implicit_affinity(total)
        if bounded != 0.0:
            normalized[label] = bounded
    return MappingProxyType(normalized)
