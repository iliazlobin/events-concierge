"""Focused mapping and bound checks for the consumer request-outcome projection."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from events_concierge.adapters.postgres.consumer import _request_from_row, _validate_page
from events_concierge.domain.enums import Lane, LifecycleState, Source


def _request_row(*, linked: bool) -> SimpleNamespace:
    lifecycle_id = uuid4() if linked else None
    return SimpleNamespace(
        request_id=uuid4(),
        raw_text="find a jazz night",
        state="received",
        created_at=datetime(2026, 7, 22, 12, tzinfo=UTC),
        constraints={"categories": ["jazz"], "budget_free": True},
        outcome_lifecycle_id=lifecycle_id,
        outcome_canonical_event_id=uuid4(),
        outcome_title="Small room jazz",
        outcome_start_at=datetime(2026, 7, 25, 19, tzinfo=UTC),
        outcome_state="scheduled",
        outcome_lane="handoff",
        outcome_source="luma",
        outcome_updated_at=datetime(2026, 7, 22, 13, tzinfo=UTC),
    )


def test_request_row_maps_only_an_explicit_selected_outcome() -> None:
    linked = _request_from_row(_request_row(linked=True))  # type: ignore[arg-type]
    unlinked = _request_from_row(_request_row(linked=False))  # type: ignore[arg-type]

    assert linked.outcome is not None
    assert linked.outcome.title == "Small room jazz"
    assert linked.outcome.state is LifecycleState.SCHEDULED
    assert linked.outcome.lane is Lane.HANDOFF
    assert linked.outcome.source is Source.LUMA
    assert unlinked.outcome is None

    projected = asdict(linked)
    rendered_keys = repr(projected)
    assert "workflow_id" not in rendered_keys
    assert "completion_token" not in rendered_keys
    assert "capability" not in rendered_keys


@pytest.mark.parametrize(
    ("offset", "limit"),
    ((-1, 1), (0, 0), (0, 52)),
)
def test_request_projection_preserves_page_bounds(offset: int, limit: int) -> None:
    with pytest.raises(ValueError, match="bounded range"):
        _validate_page(offset, limit)
