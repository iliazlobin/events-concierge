"""Saved catalog filter selections: tenant isolation, ordering, naming, and the cap.

A saved filter is presentation state, so the store is a FORCE-RLS satellite the application writes
directly. These tests hold the two properties that make that safe -- one tenant can never read or
delete another's selections -- plus the ordering and uniqueness the picker depends on.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.postgres.saved_catalog_filters import (
    PostgresSavedCatalogFilterRepository,
)
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.ports.saved_catalog_filters import MAX_SAVED_CATALOG_FILTERS

pytestmark = pytest.mark.integration


def _tenant(prefix: str) -> Tenant:
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|{tag}",
        notify_email=f"{tag}@example.test",
        relay_inbox=f"{tag}@u.example.test",
    )


def _payload(city: str = "sanfrancisco") -> dict[str, object]:
    return {
        "query": "",
        "sort": "soonest",
        "datePreset": "week",
        "customStart": "",
        "customEnd": "",
        "dateRanges": [{"id": "a", "start": "2026-08-24", "end": "2026-08-30"}],
        "sourceKeys": [],
        "city": city,
        "cities": [city],
        "locationScopes": [],
        "price": "any",
        "priceComparison": "any",
        "priceMinDollars": "",
        "priceMaxDollars": "",
        "topics": ["ai"],
    }


async def _provision(tenant: Tenant) -> None:
    await PostgresTenantRepository().add(tenant)


async def test_saved_filters_are_tenant_isolated_and_ordered_by_recent_use(db: None) -> None:
    owner, other = _tenant("saved-owner"), _tenant("saved-other")
    await _provision(owner)
    await _provision(other)
    repository = PostgresSavedCatalogFilterRepository()

    first = await repository.save_filter(
        owner.tenant_id, name="SF this week", payload=_payload()
    )
    second = await repository.save_filter(
        owner.tenant_id, name="Oakland this week", payload=_payload("oakland")
    )
    await repository.save_filter(
        other.tenant_id, name="Someone else's", payload=_payload("berkeley")
    )

    # Newest use first, and the other tenant's selection is not visible at all.
    owned = await repository.list_filters(owner.tenant_id)
    assert [saved.name for saved in owned] == ["Oakland this week", "SF this week"]
    assert owned[1].payload == _payload()

    # Applying the older one moves it to the front without editing it.
    touched = await repository.touch_filter(owner.tenant_id, first.saved_filter_id)
    assert touched.payload == _payload()
    assert touched.updated_at == first.updated_at
    reordered = await repository.list_filters(owner.tenant_id)
    assert [saved.name for saved in reordered] == ["SF this week", "Oakland this week"]

    # The other tenant sees only its own, and cannot reach across by id.
    assert [saved.name for saved in await repository.list_filters(other.tenant_id)] == [
        "Someone else's"
    ]
    with pytest.raises(ValueError, match="not found"):
        await repository.touch_filter(other.tenant_id, second.saved_filter_id)
    assert await repository.delete_filter(other.tenant_id, second.saved_filter_id) is False
    assert len(await repository.list_filters(owner.tenant_id)) == 2


async def test_a_name_is_unique_per_tenant_but_not_across_tenants(db: None) -> None:
    owner, other = _tenant("saved-name-owner"), _tenant("saved-name-other")
    await _provision(owner)
    await _provision(other)
    repository = PostgresSavedCatalogFilterRepository()

    await repository.save_filter(owner.tenant_id, name="Weeknights", payload=_payload())
    with pytest.raises(ValueError, match="already used"):
        await repository.save_filter(owner.tenant_id, name="Weeknights", payload=_payload())
    # The same label under a different account is a different thing entirely.
    await repository.save_filter(other.tenant_id, name="Weeknights", payload=_payload())

    stored = (await repository.list_filters(owner.tenant_id))[0]
    renamed = await repository.save_filter(
        owner.tenant_id,
        name="Weeknights near me",
        payload=_payload("oakland"),
        saved_filter_id=stored.saved_filter_id,
    )
    assert renamed.saved_filter_id == stored.saved_filter_id
    assert renamed.name == "Weeknights near me"
    assert renamed.payload == _payload("oakland")


async def test_a_tenant_cannot_collect_unbounded_selections(db: None) -> None:
    owner = _tenant("saved-cap")
    await _provision(owner)
    repository = PostgresSavedCatalogFilterRepository()

    for index in range(MAX_SAVED_CATALOG_FILTERS):
        await repository.save_filter(owner.tenant_id, name=f"Filter {index}", payload=_payload())
    with pytest.raises(ValueError, match="too many"):
        await repository.save_filter(owner.tenant_id, name="One too many", payload=_payload())

    # Replacing an existing selection is not adding one, so it still works at the cap.
    stored = (await repository.list_filters(owner.tenant_id))[0]
    replaced = await repository.save_filter(
        owner.tenant_id,
        name=stored.name,
        payload=_payload("berkeley"),
        saved_filter_id=stored.saved_filter_id,
    )
    assert replaced.payload == _payload("berkeley")
    assert await repository.delete_filter(owner.tenant_id, stored.saved_filter_id) is True
    assert len(await repository.list_filters(owner.tenant_id)) == MAX_SAVED_CATALOG_FILTERS - 1


async def test_a_missing_selection_is_reported_rather_than_silently_ignored(db: None) -> None:
    owner = _tenant("saved-missing")
    await _provision(owner)
    repository = PostgresSavedCatalogFilterRepository()
    absent: UUID = uuid4()

    assert await repository.delete_filter(owner.tenant_id, absent) is False
    with pytest.raises(ValueError, match="not found"):
        await repository.touch_filter(owner.tenant_id, absent)
    with pytest.raises(ValueError, match="not found"):
        await repository.save_filter(
            owner.tenant_id, name="Ghost", payload=_payload(), saved_filter_id=absent
        )


async def test_renaming_a_selection_does_not_move_it_up_the_recency_order(db: None) -> None:
    """Editing is not using. The picker sorts on use, so a rename must not jump the queue."""
    owner = _tenant("saved-rename-order")
    await _provision(owner)
    repository = PostgresSavedCatalogFilterRepository()

    older = await repository.save_filter(owner.tenant_id, name="Older", payload=_payload())
    await repository.save_filter(owner.tenant_id, name="Newer", payload=_payload("oakland"))
    assert [saved.name for saved in await repository.list_filters(owner.tenant_id)] == [
        "Newer",
        "Older",
    ]

    renamed = await repository.save_filter(
        owner.tenant_id,
        name="Older, renamed",
        payload=_payload("berkeley"),
        saved_filter_id=older.saved_filter_id,
    )
    assert renamed.last_used_at == older.last_used_at
    assert [saved.name for saved in await repository.list_filters(owner.tenant_id)] == [
        "Newer",
        "Older, renamed",
    ]

    # Applying it, on the other hand, is exactly what recency is for.
    await repository.touch_filter(owner.tenant_id, older.saved_filter_id)
    assert [saved.name for saved in await repository.list_filters(owner.tenant_id)] == [
        "Older, renamed",
        "Newer",
    ]


async def test_a_rejected_payload_is_not_reported_as_a_name_collision(db: None) -> None:
    """A constraint the API was supposed to catch must not masquerade as a duplicate name."""
    owner = _tenant("saved-bad-payload")
    await _provision(owner)
    repository = PostgresSavedCatalogFilterRepository()

    with pytest.raises(Exception) as caught:
        await repository.save_filter(owner.tenant_id, name="   ", payload=_payload())
    assert "already used" not in str(caught.value)
