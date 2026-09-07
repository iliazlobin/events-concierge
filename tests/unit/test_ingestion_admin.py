"""Ingestion-admin application boundary and safe command-worker tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshResult,
)
from events_concierge.application.ingestion_admin import IngestionAdminService
from events_concierge.domain.catalog_browse import (
    CatalogBrowseEvent,
    CatalogBrowseProvider,
    CatalogBrowseSource,
)
from events_concierge.domain.catalog_sources import CatalogRefreshDue, CatalogSource
from events_concierge.domain.enums import (
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventEntityProfile, GeoPoint
from events_concierge.domain.ingestion_admin import (
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandLease,
    IngestionCommandRunTarget,
    IngestionCommandStatus,
    IngestionOverview,
    IngestionOverviewSummary,
    IngestionPolicyStatus,
    IngestionRunPage,
    IngestionSourceConfigurationUpdate,
    IngestionSourceEnabledBulkUpdate,
    IngestionSourcePage,
    IngestionSourceRevisionTarget,
    SafeCommandResult,
)
from events_concierge.ports.ingestion_admin import (
    IngestionCommandRejectedError,
    IngestionSourceConfigurationUnavailableError,
)

_NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


class _Repository:
    def __init__(
        self,
        *,
        leases: list[IngestionCommandLease] | None = None,
        due: list[CatalogRefreshDue] | None = None,
        complete_results: list[bool] | None = None,
        defer_results: list[bool] | None = None,
        renew_results: list[bool] | None = None,
        renew_errors: list[Exception] | None = None,
        renew_hook: Callable[[], None] | None = None,
    ) -> None:
        self.leases = list(leases or [])
        self.due = list(due or [])
        self.complete_results = list(complete_results or [])
        self.defer_results = list(defer_results or [])
        self.renew_results = list(renew_results or [])
        self.renew_errors = list(renew_errors or [])
        self.renew_hook = renew_hook
        self.claims: list[tuple[int, int, str, str | None]] = []
        self.renewed: list[tuple[IngestionCommandLease, int]] = []
        self.completed: list[tuple[IngestionCommandLease, SafeCommandResult]] = []
        self.deferred: list[tuple[IngestionCommandLease, int]] = []
        self.failed: list[tuple[IngestionCommandLease, str]] = []
        self.due_calls: list[tuple[datetime, int]] = []
        self.enqueues: list[
            tuple[UUID, IngestionCommandAction, str | None, str, str, str | None]
        ] = []
        self.configuration_updates: list[dict[str, object]] = []
        self.enabled_updates: list[dict[str, object]] = []
        self.command_run_plans: list[
            tuple[UUID, int, UUID, tuple[IngestionCommandRunTarget, ...]]
        ] = []
        self.source_calls: list[dict[str, object]] = []
        self.overview_value = IngestionOverview(
            generated_at=_NOW,
            policy=IngestionPolicyStatus(True, "allowed", "allowed"),
            summary=IngestionOverviewSummary(1, 1, 1, 0, 0, 2, 0, 0),
            latest_success_at=_NOW - timedelta(hours=1),
        )

    async def overview(self) -> IngestionOverview:
        return self.overview_value

    async def list_sources(
        self,
        *,
        query: str | None,
        state: str,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        source_key: str | None,
        include_fixtures: bool,
        sort_by: str,
        sort_direction: str,
        limit: int,
        offset: int,
    ) -> IngestionSourcePage:
        self.source_calls.append(
            {
                "query": query,
                "state": state,
                "mode": mode,
                "publisher": publisher,
                "region": region,
                "source_key": source_key,
                "include_fixtures": include_fixtures,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
                "limit": limit,
                "offset": offset,
            }
        )
        return IngestionSourcePage((), 0, limit, offset)

    async def list_runs(
        self,
        *,
        status: str | None,
        source_key: str | None,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        window_hours: int | None,
        include_fixtures: bool,
        limit: int,
        offset: int,
    ) -> IngestionRunPage:
        del status, source_key, mode, publisher, region, window_hours, include_fixtures
        return IngestionRunPage((), 0, limit, offset)

    async def list_commands(self, limit: int) -> tuple[IngestionCommand, ...]:
        del limit
        return ()

    async def update_source_configuration(
        self,
        source_key: str,
        **kwargs: object,
    ) -> IngestionSourceConfigurationUpdate:
        self.configuration_updates.append({"source_key": source_key, **kwargs})
        return IngestionSourceConfigurationUpdate(
            source_key=source_key,
            source_revision=int(kwargs["expected_revision"]) + 1,
            reviewed_at=_NOW,
            updated_at=_NOW,
        )

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str,
    ) -> IngestionSourceEnabledBulkUpdate:
        self.enabled_updates.append(
            {
                "targets": targets,
                "enabled": enabled,
                "requested_by": requested_by,
            }
        )
        items = tuple(
            IngestionSourceConfigurationUpdate(
                source_key=target.source_key,
                source_revision=target.expected_revision + 1,
                reviewed_at=_NOW,
                updated_at=_NOW,
            )
            for target in targets
        )
        return IngestionSourceEnabledBulkUpdate(
            enabled=enabled,
            requested=len(targets),
            updated=len(targets),
            unchanged=0,
            items=items,
        )

    async def get_command(self, command_id: UUID) -> IngestionCommand | None:
        del command_id
        return None

    async def get_command_detail(self, command_id: UUID) -> None:
        del command_id

    async def link_command_runs(
        self,
        command_id: UUID,
        attempt_count: int,
        lease_token: UUID,
        targets: tuple[IngestionCommandRunTarget, ...],
    ) -> None:
        self.command_run_plans.append((command_id, attempt_count, lease_token, targets))

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        self.due_calls.append((now, limit))
        return self.due[:limit]

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction,
        source_key: str | None,
        requested_by: str,
        release_revision: str,
        image_digest: str | None,
    ) -> IngestionCommand:
        self.enqueues.append(
            (
                command_id,
                action,
                source_key,
                requested_by,
                release_revision,
                image_digest,
            )
        )
        return _command(command_id, action, source_key)

    async def claim_batch(
        self,
        limit: int,
        lease_seconds: int,
        executor_release_revision: str,
        executor_image_digest: str | None,
    ) -> tuple[IngestionCommandLease, ...]:
        self.claims.append(
            (
                limit,
                lease_seconds,
                executor_release_revision,
                executor_image_digest,
            )
        )
        if not self.leases:
            return ()
        return (self.leases.pop(0),)

    async def renew_lease(
        self,
        lease: IngestionCommandLease,
        lease_seconds: int,
    ) -> bool:
        self.renewed.append((lease, lease_seconds))
        if self.renew_hook is not None:
            self.renew_hook()
        if self.renew_errors:
            raise self.renew_errors.pop(0)
        return self.renew_results.pop(0) if self.renew_results else True

    async def complete(
        self,
        lease: IngestionCommandLease,
        result: SafeCommandResult,
    ) -> bool:
        self.completed.append((lease, result))
        return self.complete_results.pop(0) if self.complete_results else True

    async def fail(self, lease: IngestionCommandLease, error_code: str) -> bool:
        self.failed.append((lease, error_code))
        return True

    async def defer(
        self,
        lease: IngestionCommandLease,
        retry_after_seconds: int,
    ) -> bool:
        self.deferred.append((lease, retry_after_seconds))
        return self.defer_results.pop(0) if self.defer_results else True


class _Router:
    def __init__(
        self,
        outcomes: dict[str, CatalogRefreshOutcome] | None = None,
        failures: set[str] | None = None,
        retry_delays: dict[str, float] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.failures = failures or set()
        self.retry_delays = retry_delays or {}
        self.calls: list[tuple[str, str]] = []

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key))
        if source_key in self.failures:
            raise RuntimeError("unreviewed provider response must not enter admin state")
        return CatalogRefreshResult(
            source_key=source_key,
            run_key=run_key,
            outcome=self.outcomes.get(source_key, CatalogRefreshOutcome.SUCCEEDED),
            candidate_count=7,
            canonical_count=5,
            detail="sensitive adapter detail",
            retry_after_seconds=self.retry_delays.get(source_key, 1.2),
        )


class _Catalog:
    def __init__(self, events: list[CatalogBrowseEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    async def browse_current(
        self, **kwargs: object
    ) -> tuple[
        list[CatalogBrowseEvent],
        list[CatalogBrowseProvider],
    ]:
        self.calls.append(kwargs)
        return (
            self.events,
            [
                CatalogBrowseProvider(
                    source_key="approved-source",
                    display_name="Approved source",
                    publisher="Publisher",
                    provider="luma",
                    seed_url="https://example.test/events",
                    event_count=67,
                )
            ],
        )


async def test_read_alias_and_enqueue_alias_delegate_closed_inputs() -> None:
    repository = _Repository()
    service = IngestionAdminService(repository)
    command_id = uuid4()

    assert await service.get_overview() == repository.overview_value
    command = await service.enqueue_command(
        command_id,
        "refresh_source",
        "approved-source",
    )

    assert command.command_id == command_id
    assert repository.enqueues == [
        (
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            "approved-source",
            "local-admin",
            "development",
            None,
        )
    ]


async def test_source_sort_is_closed_and_forwarded_to_the_repository() -> None:
    repository = _Repository()
    service = IngestionAdminService(repository)

    page = await service.list_sources(
        query="reviewed",
        sort_by="output",
        sort_direction="desc",
        limit=25,
        offset=50,
    )

    assert page.limit == 25
    assert page.offset == 50
    assert repository.source_calls == [
        {
            "query": "reviewed",
            "state": "all",
            "mode": None,
            "publisher": None,
            "region": None,
            "source_key": None,
            "include_fixtures": False,
            "sort_by": "output",
            "sort_direction": "desc",
            "limit": 25,
            "offset": 50,
        }
    ]

    with pytest.raises(ValueError, match="sort field"):
        await service.list_sources(sort_by="seed_url")
    with pytest.raises(ValueError, match="sort direction"):
        await service.list_sources(sort_direction="sideways")


async def test_source_configuration_update_validates_the_reviewed_contract() -> None:
    repository = _Repository()
    service = IngestionAdminService(repository, now=lambda: _NOW)

    updated = await service.update_source_configuration(
        "approved-source",
        expected_revision=4,
        seed_url="https://events.example.test/calendar",
        approved_origins=("https://events.example.test",),
        mode="public_jsonld",
        enabled=True,
        handoff_only=True,
        review_expires_at=_NOW + timedelta(days=30),
        refresh_interval_minutes=90,
        min_interval_ms=2_000,
        page_limit=2,
        requested_by="local-admin",
    )

    assert updated.source_revision == 5
    assert repository.configuration_updates == [
        {
            "source_key": "approved-source",
            "expected_revision": 4,
            "seed_url": "https://events.example.test/calendar",
            "approved_origins": ("https://events.example.test",),
            "mode": "public_jsonld",
            "enabled": True,
            "handoff_only": True,
            "review_expires_at": _NOW + timedelta(days=30),
            "refresh_interval_minutes": 90,
            "min_interval_ms": 2_000,
            "page_limit": 2,
            "requested_by": "local-admin",
        }
    ]

    with pytest.raises(ValueError, match="handoff-only"):
        await service.update_source_configuration(
            "approved-source",
            expected_revision=4,
            seed_url="https://events.example.test/calendar",
            approved_origins=("https://events.example.test",),
            mode="public_jsonld",
            enabled=True,
            handoff_only=False,
            review_expires_at=None,
            refresh_interval_minutes=90,
            min_interval_ms=2_000,
            page_limit=2,
        )


async def test_bulk_source_enabled_update_is_exact_unique_and_bounded() -> None:
    repository = _Repository()
    service = IngestionAdminService(repository, now=lambda: _NOW)
    targets = (
        IngestionSourceRevisionTarget("approved-source", 4),
        IngestionSourceRevisionTarget("second-source", 7),
    )

    result = await service.set_sources_enabled(
        targets,
        enabled=False,
        requested_by="local-admin",
    )

    assert result.requested == 2
    assert result.updated == 2
    assert [item.source_revision for item in result.items] == [5, 8]
    assert repository.enabled_updates == [
        {
            "targets": targets,
            "enabled": False,
            "requested_by": "local-admin",
        }
    ]

    invalid_targets = (
        (),
        (IngestionSourceRevisionTarget("approved-source", 4),) * 101,
        (
            IngestionSourceRevisionTarget("approved-source", 4),
            IngestionSourceRevisionTarget("approved-source", 5),
        ),
        (IngestionSourceRevisionTarget("approved-source", 0),),
        (IngestionSourceRevisionTarget("approved-source", 2_147_483_648),),
    )
    for invalid in invalid_targets:
        with pytest.raises(ValueError):
            await service.set_sources_enabled(invalid, enabled=True)

    with pytest.raises(ValueError, match="enabled state"):
        await service.set_sources_enabled(targets, enabled=1)  # type: ignore[arg-type]

    with pytest.raises(IngestionSourceConfigurationUnavailableError):
        await service.set_sources_enabled(
            (
                IngestionSourceRevisionTarget(
                    "alameda-county-library-fremont-events",
                    3,
                ),
            ),
            enabled=True,
        )


async def test_source_event_projection_is_paged_searchable_and_reports_field_gaps() -> None:
    event_id = uuid4()
    event = CatalogBrowseEvent(
        canonical_event=CanonicalEvent(
            canonical_event_id=event_id,
            title="Parsed event",
            start_at=_NOW + timedelta(days=1),
            venue_name=None,
            geo=GeoPoint(37.77, -122.42),
            city_norm="sanfrancisco",
            description="x" * 2_050,
            price_status=PriceStatus.PAID,
            price_min_cents=2_500,
            price_max_cents=5_000,
            price_currency="USD",
            normalizer_version=2,
            merge_version=3,
            organizer_name="Example Organizer",
            entity_profiles=(
                EventEntityProfile(
                    name="Example Organizer",
                    role="organizer",
                    kind="organization",
                    profile_url="https://www.linkedin.com/company/example-organizer",
                ),
            ),
            attendance_count=80,
            registration_status=RegistrationStatus.WAITLIST,
        ),
        sources=(
            CatalogBrowseSource(
                source_key="approved-source",
                label="Approved source",
                publisher="Publisher",
                provider="luma",
                seed_url="https://example.test/events",
                source=Source.PUBLIC_JSONLD,
                source_event_id="provider-42",
                registration_url="https://example.test/event/42",
                last_seen_at=_NOW,
                refresh_run_key="manual:approved-source",
            ),
        ),
    )
    catalog = _Catalog([event, event])
    service = IngestionAdminService(_Repository(), catalog=catalog)

    page = await service.list_source_events(
        "approved-source",
        query=" parsed ",
        limit=1,
    )

    assert page.source_total == 67
    assert page.has_more is True
    assert page.next_start_at == event.canonical_event.start_at
    assert page.next_canonical_event_id == event_id
    assert page.query == "parsed"
    assert len(page.items) == 1
    projected = page.items[0]
    assert len(projected.description) == 2_000
    assert projected.description_length == 2_050
    assert [issue.value for issue in projected.quality_issues] == [
        "missing_end_time",
        "missing_venue",
    ]
    assert projected.source_event_id == "provider-42"
    assert projected.normalizer_version == 2
    assert projected.merge_version == 3
    assert (
        projected.price_min_cents,
        projected.price_max_cents,
        projected.price_currency,
    ) == (2_500, 5_000, "USD")
    assert projected.entity_profiles == event.canonical_event.entity_profiles
    assert projected.attendance_count == 80
    assert projected.registration_status == "waitlist"
    assert catalog.calls == [
        {
            "source_keys": ("approved-source",),
            "after": None,
            "limit": 2,
            "query": "parsed",
        }
    ]


@pytest.mark.parametrize(
    ("action", "source_key"),
    [
        ("delete_source", None),
        ("refresh_source", None),
        ("refresh_source", "TEST-invalid"),
        ("refresh_due", "approved-source"),
    ],
)
async def test_enqueue_rejects_every_shape_outside_fixed_commands(
    action: str,
    source_key: str | None,
) -> None:
    service = IngestionAdminService(_Repository())

    with pytest.raises(IngestionCommandRejectedError) as raised:
        await service.enqueue_command(uuid4(), action, source_key)

    assert raised.value.code == "invalid_command"


async def test_source_command_uses_stable_run_key_and_excludes_private_detail() -> None:
    command_id = uuid4()
    lease = _lease(command_id, IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    repository = _Repository(leases=[lease])
    router = _Router()
    service = IngestionAdminService(repository, router, now=lambda: _NOW)

    report = await service.process_once(limit=2)

    expected_run_key = f"admin:{command_id}"
    assert router.calls == [("approved-source", expected_run_key)]
    assert repository.command_run_plans == [
        (
            command_id,
            lease.attempt_count,
            lease.lease_token,
            (
                IngestionCommandRunTarget(
                    position=0,
                    source_key="approved-source",
                    run_key=expected_run_key,
                ),
            ),
        )
    ]
    assert repository.claims == [
        (1, 300, "development", None),
        (1, 300, "development", None),
    ]
    assert repository.completed == [
        (
            lease,
            {
                "action": "refresh_source",
                "source_key": "approved-source",
                "run_key": expected_run_key,
                "outcome": "succeeded",
                "candidate_count": 7,
                "canonical_count": 5,
                "retry_after_seconds": 2,
            },
        )
    ]
    assert report.claimed == 1
    assert report.completed == 1
    assert report.failed == 0
    assert report.lost_leases == 0


async def test_source_command_links_claim_attempt_beyond_deferred_retry_policy_cap() -> None:
    command_id = uuid4()
    lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_SOURCE,
        source_key="approved-source",
        attempt_count=51,
        lease_token=uuid4(),
    )
    repository = _Repository(leases=[lease])
    router = _Router()

    report = await IngestionAdminService(repository, router, now=lambda: _NOW).process_once()

    assert repository.command_run_plans[0][1] == 51
    assert repository.completed[0][0] == lease
    assert report.completed == 1


async def test_due_command_dispatches_only_admin_repository_due_projection() -> None:
    command_id = uuid4()
    lease = _lease(command_id, IngestionCommandAction.REFRESH_DUE)
    alpha = _due("alpha-source", _NOW - timedelta(minutes=2))
    beta = _due("beta-source", _NOW - timedelta(minutes=1))
    repository = _Repository(leases=[lease], due=[beta, alpha])
    router = _Router(
        {"alpha-source": CatalogRefreshOutcome.QUEUED},
        failures={"beta-source"},
    )
    service = IngestionAdminService(
        repository,
        router,
        cadence_batch_size=500,
        now=lambda: _NOW,
    )

    report = await service.process_once()

    assert repository.due_calls == [(_NOW, 500)]
    assert repository.command_run_plans == [
        (
            command_id,
            lease.attempt_count,
            lease.lease_token,
            (
                IngestionCommandRunTarget(0, "alpha-source", alpha.run_key()),
                IngestionCommandRunTarget(1, "beta-source", beta.run_key()),
            ),
        )
    ]
    assert router.calls == [
        ("alpha-source", alpha.run_key()),
        ("beta-source", beta.run_key()),
    ]
    assert repository.completed[0][1] == {
        "action": "refresh_due",
        "due_sources": 2,
        "attempted": 2,
        "succeeded": 0,
        "queued": 1,
        "skipped": 0,
        "deferred": 0,
        "already_succeeded": 0,
        "busy": 0,
        "progressed": 0,
        "failed": 1,
    }
    assert report.completed == 1


async def test_deferred_due_command_retries_the_fleet_after_the_longest_pacer_wait() -> None:
    command_id = uuid4()
    lease = _lease(command_id, IngestionCommandAction.REFRESH_DUE)
    alpha = _due("alpha-source", _NOW - timedelta(minutes=2))
    beta = _due("beta-source", _NOW - timedelta(minutes=1))
    repository = _Repository(leases=[lease], due=[alpha, beta])
    router = _Router(
        {
            "alpha-source": CatalogRefreshOutcome.DEFERRED,
            "beta-source": CatalogRefreshOutcome.DEFERRED,
        },
        retry_delays={"alpha-source": 1.2, "beta-source": 4.1},
    )

    report = await IngestionAdminService(repository, router, now=lambda: _NOW).process_once()

    assert repository.completed == []
    assert repository.deferred == [(lease, 5)]
    assert report.claimed == 1
    assert report.deferred == 1
    assert report.completed == 0


async def test_due_retry_composes_with_idempotent_temporal_queue_outcomes() -> None:
    command_id = uuid4()
    first_lease = _lease(command_id, IngestionCommandAction.REFRESH_DUE)
    second_lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_DUE,
        source_key=None,
        attempt_count=2,
        lease_token=uuid4(),
    )
    temporal = _due("alpha-source", _NOW - timedelta(minutes=2))
    direct = _due("beta-source", _NOW - timedelta(minutes=1))
    repository = _Repository(leases=[first_lease, second_lease], due=[temporal, direct])

    class _MixedRouter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.direct_attempts = 0

        async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
            self.calls.append((source_key, run_key))
            if source_key == temporal.source.source_key:
                outcome = CatalogRefreshOutcome.QUEUED
            else:
                self.direct_attempts += 1
                outcome = (
                    CatalogRefreshOutcome.DEFERRED
                    if self.direct_attempts == 1
                    else CatalogRefreshOutcome.SUCCEEDED
                )
            return CatalogRefreshResult(
                source_key=source_key,
                run_key=run_key,
                outcome=outcome,
                retry_after_seconds=2.1 if outcome is CatalogRefreshOutcome.DEFERRED else None,
            )

    router = _MixedRouter()
    report = await IngestionAdminService(repository, router, now=lambda: _NOW).process_once(limit=2)

    expected_calls = [
        ("alpha-source", temporal.run_key()),
        ("beta-source", direct.run_key()),
    ]
    assert router.calls == [*expected_calls, *expected_calls]
    assert repository.deferred == [(first_lease, 3)]
    assert len(repository.completed) == 1
    assert repository.completed[0][0] == second_lease
    assert repository.completed[0][1]["queued"] == 1
    assert repository.completed[0][1]["succeeded"] == 1
    assert repository.completed[0][1]["deferred"] == 0
    assert report.claimed == 2
    assert report.deferred == 1
    assert report.completed == 1


async def test_deferred_due_command_retries_past_source_limit_and_completes() -> None:
    command_id = uuid4()
    first_lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_DUE,
        source_key=None,
        attempt_count=5,
        lease_token=uuid4(),
    )
    second_lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_DUE,
        source_key=None,
        attempt_count=6,
        lease_token=uuid4(),
    )
    due = _due("alpha-source", _NOW - timedelta(minutes=1))
    repository = _Repository(leases=[first_lease, second_lease], due=[due])

    class _EventuallySuccessfulRouter:
        def __init__(self) -> None:
            self.attempts = 0

        async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
            self.attempts += 1
            outcome = (
                CatalogRefreshOutcome.DEFERRED
                if self.attempts == 1
                else CatalogRefreshOutcome.SUCCEEDED
            )
            return CatalogRefreshResult(
                source_key=source_key,
                run_key=run_key,
                outcome=outcome,
                retry_after_seconds=1.2 if outcome is CatalogRefreshOutcome.DEFERRED else None,
            )

    router = _EventuallySuccessfulRouter()
    report = await IngestionAdminService(repository, router, now=lambda: _NOW).process_once(limit=2)

    assert repository.deferred == [(first_lease, 2)]
    assert repository.completed == [
        (
            second_lease,
            {
                "action": "refresh_due",
                "due_sources": 1,
                "attempted": 1,
                "succeeded": 1,
                "queued": 0,
                "skipped": 0,
                "deferred": 0,
                "already_succeeded": 0,
                "busy": 0,
                "progressed": 0,
                "failed": 0,
            },
        )
    ]
    assert router.attempts == 2
    assert report.claimed == 2
    assert report.deferred == 1
    assert report.completed == 1


async def test_deferred_due_command_stops_after_the_fleet_attempt_limit() -> None:
    command_id = uuid4()
    lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_DUE,
        source_key=None,
        attempt_count=50,
        lease_token=uuid4(),
    )
    due = _due("alpha-source", _NOW - timedelta(minutes=1))
    repository = _Repository(leases=[lease], due=[due])
    router = _Router({"alpha-source": CatalogRefreshOutcome.DEFERRED})

    report = await IngestionAdminService(repository, router, now=lambda: _NOW).process_once()

    assert repository.deferred == []
    assert repository.completed[0][1] == {
        "action": "refresh_due",
        "due_sources": 1,
        "attempted": 1,
        "succeeded": 0,
        "queued": 0,
        "skipped": 0,
        "deferred": 1,
        "already_succeeded": 0,
        "busy": 0,
        "progressed": 0,
        "failed": 0,
        "retry_after_seconds": 2,
    }
    assert report.completed == 1
    assert report.deferred == 0


async def test_deferred_source_command_is_durably_requeued_with_the_same_run_key() -> None:
    command_id = uuid4()
    lease = _lease(command_id, IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    repository = _Repository(leases=[lease])
    router = _Router({"approved-source": CatalogRefreshOutcome.DEFERRED})

    report = await IngestionAdminService(repository, router).process_once()

    assert router.calls == [("approved-source", f"admin:{command_id}")]
    assert repository.deferred == [(lease, 2)]
    assert repository.completed == []
    assert report.claimed == 1
    assert report.deferred == 1
    assert report.completed == 0
    assert report.lost_leases == 0


async def test_deferred_source_command_stops_after_the_bounded_attempt_limit() -> None:
    command_id = uuid4()
    lease = IngestionCommandLease(
        command_id=command_id,
        action=IngestionCommandAction.REFRESH_SOURCE,
        source_key="approved-source",
        attempt_count=5,
        lease_token=uuid4(),
    )
    repository = _Repository(leases=[lease])
    router = _Router({"approved-source": CatalogRefreshOutcome.DEFERRED})

    report = await IngestionAdminService(repository, router).process_once()

    assert repository.deferred == []
    assert repository.completed[0][1]["outcome"] == "deferred"
    assert report.completed == 1
    assert report.deferred == 0


async def test_deferred_source_command_reports_a_lost_lease_without_completing() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    repository = _Repository(leases=[lease], defer_results=[False])
    router = _Router({"approved-source": CatalogRefreshOutcome.DEFERRED})

    report = await IngestionAdminService(repository, router).process_once()

    assert repository.completed == []
    assert report.deferred == 0
    assert report.lost_leases == 1


async def test_routerless_worker_fails_claimed_command_with_fixed_code() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    repository = _Repository(leases=[lease])

    report = await IngestionAdminService(repository).process_once()

    assert repository.failed == [(lease, "worker_unavailable")]
    assert report.failed == 1


async def test_execution_failure_never_persists_exception_text() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "broken-source")
    repository = _Repository(leases=[lease])
    router = _Router(failures={"broken-source"})

    report = await IngestionAdminService(repository, router).process_once()

    assert repository.failed == [(lease, "source_refresh_failed")]
    assert report.failed == 1


async def test_long_execution_renews_its_exact_command_lease() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    release_operation = asyncio.Event()

    class _BlockingRouter:
        async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
            await release_operation.wait()
            return CatalogRefreshResult(
                source_key=source_key,
                run_key=run_key,
                outcome=CatalogRefreshOutcome.SUCCEEDED,
            )

    repository = _Repository(
        leases=[lease],
        renew_results=[True],
        renew_hook=release_operation.set,
    )
    service = IngestionAdminService(
        repository,
        _BlockingRouter(),
        lease_seconds=300,
        lease_heartbeat_seconds=0.001,
    )

    report = await asyncio.wait_for(service.process_once(), timeout=1)

    assert repository.renewed == [(lease, 300)]
    assert len(repository.completed) == 1
    assert report.completed == 1
    assert report.lost_leases == 0


async def test_lost_heartbeat_cancels_execution_without_terminally_mutating_command() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    never_release = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingRouter:
        async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
            del source_key, run_key
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    repository = _Repository(leases=[lease], renew_results=[False])
    service = IngestionAdminService(
        repository,
        _BlockingRouter(),
        lease_seconds=300,
        lease_heartbeat_seconds=0.001,
    )

    report = await asyncio.wait_for(service.process_once(), timeout=1)

    assert repository.renewed == [(lease, 300)]
    assert cancelled.is_set()
    assert repository.completed == []
    assert repository.deferred == []
    assert repository.failed == []
    assert report.lost_leases == 1


async def test_unknown_heartbeat_result_is_treated_as_lost_authority() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    never_release = asyncio.Event()
    cancelled = asyncio.Event()

    class _BlockingRouter:
        async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
            del source_key, run_key
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    repository = _Repository(
        leases=[lease],
        renew_errors=[TimeoutError("database reply was not observed")],
    )
    service = IngestionAdminService(
        repository,
        _BlockingRouter(),
        lease_seconds=300,
        lease_heartbeat_seconds=0.001,
    )

    report = await asyncio.wait_for(service.process_once(), timeout=1)

    assert cancelled.is_set()
    assert repository.completed == []
    assert repository.failed == []
    assert report.lost_leases == 1


async def test_rejected_completion_is_terminally_failed_when_lease_remains_live() -> None:
    lease = _lease(uuid4(), IngestionCommandAction.REFRESH_SOURCE, "approved-source")
    repository = _Repository(leases=[lease], complete_results=[False])

    report = await IngestionAdminService(repository, _Router()).process_once()

    assert repository.failed == [(lease, "invalid_result")]
    assert report.failed == 1
    assert report.lost_leases == 0


def test_constructor_aligns_command_and_cadence_bounds() -> None:
    repository = _Repository()

    IngestionAdminService(repository, lease_seconds=300, cadence_batch_size=500)
    IngestionAdminService(repository, lease_seconds=21_600, cadence_batch_size=1)
    IngestionAdminService(repository, deferred_attempt_limit=1)
    IngestionAdminService(repository, fleet_deferred_attempt_limit=1)
    IngestionAdminService(repository, fleet_deferred_attempt_limit=50)
    IngestionAdminService(
        repository,
        lease_seconds=300,
        lease_heartbeat_seconds=299.9,
    )
    with pytest.raises(ValueError):
        IngestionAdminService(repository, lease_seconds=299)
    for invalid_heartbeat in (0.0, -1.0, 300.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            IngestionAdminService(
                repository,
                lease_seconds=300,
                lease_heartbeat_seconds=invalid_heartbeat,
            )
    with pytest.raises(ValueError):
        IngestionAdminService(repository, cadence_batch_size=501)
    with pytest.raises(ValueError):
        IngestionAdminService(repository, deferred_attempt_limit=0)
    with pytest.raises(ValueError):
        IngestionAdminService(repository, deferred_attempt_limit=6)
    with pytest.raises(ValueError):
        IngestionAdminService(repository, fleet_deferred_attempt_limit=0)
    with pytest.raises(ValueError):
        IngestionAdminService(repository, fleet_deferred_attempt_limit=51)


def _lease(
    command_id: UUID,
    action: IngestionCommandAction,
    source_key: str | None = None,
) -> IngestionCommandLease:
    return IngestionCommandLease(
        command_id=command_id,
        action=action,
        source_key=source_key,
        attempt_count=1,
        lease_token=uuid4(),
    )


def _command(
    command_id: UUID,
    action: IngestionCommandAction,
    source_key: str | None,
) -> IngestionCommand:
    return IngestionCommand(
        command_id=command_id,
        action=action,
        source_key=source_key,
        status=IngestionCommandStatus.QUEUED,
        requested_at=_NOW,
        started_at=None,
        completed_at=None,
        result=None,
        error_code=None,
    )


def _due(source_key: str, due_at: datetime) -> CatalogRefreshDue:
    source = CatalogSource(
        source_key=source_key,
        display_name=f"{source_key} events",
        publisher="Reviewed Publisher",
        seed_url=f"https://events.example.com/{source_key}",
        approved_origins=("https://events.example.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=due_at - timedelta(days=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    return CatalogRefreshDue(
        source=source,
        due_at=due_at,
        last_succeeded_at=due_at - timedelta(minutes=60),
    )
