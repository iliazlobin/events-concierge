"""Hermetic synthetic G1-style workflow-quality matrix (P5a, FR-5.0/6.6/8.1).

This is a semantic coverage harness, not a provider benchmark or closure of G1/G2/G3.  It drives
the real Temporal parent/child spine and local PostgreSQL repositories with fixture-only source,
browser, calendar, and notification adapters.  The normalized report intentionally contains only
scenario names and aggregate counts; it excludes raw request text, UUIDs, timestamps, URLs, and
ranking scores.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from events_concierge.adapters.crawl.source import PublicJsonLdSource
from events_concierge.adapters.luma.scripted_browser import (
    ScriptedBrowserRsvpDriver,
    ScriptedBrowserScenario,
)
from events_concierge.adapters.luma.source import LumaSource
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.mock.notifier import MockNotifier
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from events_concierge.application.outbox import OutboxRelay
from events_concierge.composition import Container, build_container
from events_concierge.config import get_settings
from events_concierge.domain.conflict import BusyBlock
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import (
    ConflictVerdict,
    GroupCondition,
    Modality,
    PriceStatus,
    Source,
)
from events_concierge.domain.events import CandidateEvent, CanonicalEvent
from events_concierge.domain.ids import registration_workflow_id, request_workflow_id
from events_concierge.domain.policy import SourcePolicy
from events_concierge.domain.request import EventRequest, RequestConstraints, TimeWindow
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.browser import BrowserRsvpObservation, BrowserRsvpStatus
from events_concierge.ports.notifications import NotificationKind
from events_concierge.ports.repositories import (
    NotificationClaim,
    OutboxQueueSnapshot,
    OutboxRecord,
)
from events_concierge.ports.sources import SourcePort
from events_concierge.workflows.activities import (
    await_confirmation,
    close_failed_candidate,
    compensate_calendar_write,
    complete_lifecycle,
    dedupe_calendar,
    discover_and_rank,
    enqueue_handoff_reminder,
    expire_handoff,
    finalize_no_candidate,
    link_request_outcome,
    policy_gate,
    reconcile_organizer_change,
    register_erasure_workflow_targets,
    register_or_rsvp,
    resolve_membership,
    route_to_handoff,
    set_container,
    unrsvp,
    write_to_calendar,
)
from events_concierge.workflows.dto import RequestInput, RequestResult
from events_concierge.workflows.workflows import EventRequestWorkflow, RegistrationWorkflow

pytestmark = pytest.mark.integration

_CORPUS_PATH = Path(__file__).parents[1] / "fixtures" / "quality" / "g1-v1.json"
_FIXTURE_NOW = datetime(2039, 1, 1, tzinfo=UTC)
_SCENARIO_START = datetime(2040, 1, 1, 18, tzinfo=UTC)
_MEMBER_OVERLAY_ID = "g1-member-meetup-autonomous"
_QUALITY_LOAD_REPEATS_ENV = "EC_QUALITY_LOAD_REPEATS"
_MAX_QUALITY_LOAD_REPEATS = 5
_SOURCE_EFFECT_WAIT_SECONDS = 10.0
_REQUEST_TENANT_QUERY = text("SELECT tenant_id FROM event_requests WHERE request_id = :request_id")
_ACTION_AUDIT_PHASE_COUNTS_QUERY = text(
    """SELECT phase, count(*)::integer AS audit_count
       FROM registration_action_audit
       WHERE tenant_id = :tenant_id
       GROUP BY phase
       ORDER BY phase"""
)
_REGISTERED_AUDIT_PHASES = {
    "policy_precheck": 1,
    "policy_pre_mutate": 1,
    "source_rsvp_outcome": 1,
}


async def _seed_registration_consent(
    tenant_id: UUID,
    source: Source,
    modality: Modality,
) -> None:
    """Seed owner-recorded P14c evidence; workflows cannot manufacture delegation (FR-2.9)."""
    if (source, modality) not in {
        (Source.MEETUP, Modality.API),
        (Source.LUMA, Modality.BROWSER),
    }:
        raise ValueError("fixture consent only supports the closed P14c registration lanes")
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for registration-consent fixtures")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.tenant_source_consents (
                        consent_id, tenant_id, source, modality, scope
                    ) VALUES (
                        :consent_id, :tenant_id, :source, :modality, 'registration'
                    )
                    """
                ),
                {
                    "consent_id": uuid4(),
                    "tenant_id": tenant_id,
                    "source": source.value,
                    "modality": modality.value,
                },
            )
    finally:
        await owner_engine.dispose()


async def _seed_supported_registration_consents(
    tenant_id: UUID, register_sources: Mapping[Source, SourcePort]
) -> None:
    """Seed only fixture lanes with a real RSVP surface; crawl/handoff paths stay unseeded."""
    for source, modality in (
        (Source.MEETUP, Modality.API),
        (Source.LUMA, Modality.BROWSER),
    ):
        if source in register_sources:
            await _seed_registration_consent(tenant_id, source, modality)


@dataclass(frozen=True, slots=True)
class QualityEvent:
    """One sanitized catalog fixture; source-event keys never leave the quality report."""

    source: Source
    source_event_key: str
    title: str
    price_status: PriceStatus
    conflict_role: str | None = None


@dataclass(frozen=True, slots=True)
class QualityScenario:
    """Typed view of one versioned synthetic request journey."""

    scenario_id: str
    raw_text: str
    membership: GroupCondition
    free_only: bool
    events: tuple[QualityEvent, ...]
    expected_outcome: str
    expected_source_mutation_count: int
    expected_calendar_count: int
    expected_notification_kind: NotificationKind
    expected_visible_source_event_keys: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class QualityCorpus:
    """The synthetic corpus and its non-PII golden aggregate."""

    version: str
    scenarios: tuple[QualityScenario, ...]
    expected_report: dict[str, object]


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    """One execution outcome; the report projection deliberately excludes identifiers."""

    tenant_id: UUID
    request_id: UUID
    scenario_id: str
    outcome: str
    selected_source: str | None
    source_mutation_count: int
    calendar_count: int
    notification_kinds: tuple[str, ...]
    notification_dedup_count: int
    action_audit_count: int
    action_audit_phases: dict[str, int]
    lost_source_ack_recovered: bool = False
    duplicate_parent_start_rejected: bool = False


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Stable P5a/P6a semantic aggregate, not a latency measurement or audit export."""

    corpus_version: str
    scenario_ids: tuple[str, ...]
    outcomes: dict[str, int]
    selected_sources: dict[str, int]
    source_mutation_count: int
    calendar_count: int
    notification_kinds: dict[str, int]
    notification_dedup_count: int
    action_audit_count: int
    action_audit_phases: dict[str, int]
    fault_overlays: dict[str, int]

    def normalized(self) -> dict[str, object]:
        """Produce JSON-native aggregates only; no fixture event data can escape this boundary."""
        return {
            "corpus_version": self.corpus_version,
            "scenario_ids": list(self.scenario_ids),
            "scenario_count": len(self.scenario_ids),
            "outcomes": dict(sorted(self.outcomes.items())),
            "selected_sources": dict(sorted(self.selected_sources.items())),
            "source_mutation_count": self.source_mutation_count,
            "calendar_count": self.calendar_count,
            "notification_kinds": dict(sorted(self.notification_kinds.items())),
            "notification_dedup_count": self.notification_dedup_count,
            "action_audit_count": self.action_audit_count,
            "action_audit_phases": dict(sorted(self.action_audit_phases.items())),
            "fault_overlays": dict(sorted(self.fault_overlays.items())),
        }


@dataclass(slots=True)
class ScenarioPorts:
    """Fixture-only source graph plus observability seams for a single journey."""

    discovery_source: SourcePort | None
    register_sources: dict[Source, SourcePort]
    selected_source: Source | None
    source_mutations: Callable[[], int]
    confirming_source: ConfirmingSource | None = None
    primary_source_event_id: str | None = None


class ScenarioOutbox:
    """Tenant-only replay queue for exercising OutboxRelay without claiming global DB rows."""

    def __init__(self, batches: list[list[OutboxRecord]]) -> None:
        self._batches = batches
        self.delivered: list[int] = []
        self.notification_claims: list[str] = []

    async def queue_snapshot(self) -> OutboxQueueSnapshot:
        return OutboxQueueSnapshot(pending=0, ready=0, leased=0, oldest_ready_at=None)

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[OutboxRecord]:
        del limit, lease_seconds
        return self._batches.pop(0) if self._batches else []

    async def mark_delivered(self, record: OutboxRecord) -> bool:
        self.delivered.append(record.outbox_id)
        return True

    async def reschedule(
        self,
        record: OutboxRecord,
        *,
        retry_at: datetime | None,
        error: str,
        consume_attempt: bool,
    ) -> bool:
        del record, retry_at, error, consume_attempt
        return True

    async def claim_notification(
        self, record: OutboxRecord, *, dedup_key: str, lease_seconds: int
    ) -> NotificationClaim:
        del record, lease_seconds
        self.notification_claims.append(dedup_key)
        return NotificationClaim.ACQUIRED

    async def has_notification_send_authority(
        self, record: OutboxRecord, *, dedup_key: str
    ) -> bool:
        del record, dedup_key
        return True

    async def mark_notification_delivered(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        del record, dedup_key
        return True

    async def release_notification(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        del record, dedup_key
        return True


def _activities() -> list[object]:
    """Match the production worker's complete activity registration set."""
    return [
        discover_and_rank,
        register_erasure_workflow_targets,
        finalize_no_candidate,
        link_request_outcome,
        resolve_membership,
        policy_gate,
        close_failed_candidate,
        register_or_rsvp,
        await_confirmation,
        compensate_calendar_write,
        dedupe_calendar,
        write_to_calendar,
        reconcile_organizer_change,
        unrsvp,
        route_to_handoff,
        complete_lifecycle,
        expire_handoff,
        enqueue_handoff_reminder,
    ]


def _load_corpus() -> QualityCorpus:
    """Decode the checked-in fixture into validated, typed scenario data."""
    raw = cast(dict[str, object], json.loads(_CORPUS_PATH.read_text()))
    version = _required_str(raw, "corpus_version")
    scenario_values = _required_list(raw, "scenarios")
    scenarios = tuple(
        _scenario_from_json(cast(dict[str, object], item)) for item in scenario_values
    )
    if len(scenarios) != 8 or len({scenario.scenario_id for scenario in scenarios}) != len(
        scenarios
    ):
        raise ValueError("g1 corpus must contain exactly eight uniquely named scenarios")
    expected_report = cast(dict[str, object], raw["expected_report"])
    return QualityCorpus(version, scenarios, expected_report)


def _scenario_from_json(value: dict[str, object]) -> QualityScenario:
    events = tuple(
        _event_from_json(cast(dict[str, object], event))
        for event in _required_list(value, "events")
    )
    visibility = value.get("expected_visibility")
    visible_keys: tuple[str, ...] | None = None
    if visibility is not None:
        visibility_data = cast(dict[str, object], visibility)
        visible_keys = tuple(
            str(item) for item in _required_list(visibility_data, "visible_source_event_keys")
        )
        if bool(visibility_data.get("free_only", False)) is not bool(value.get("free_only", False)):
            raise ValueError("g1 visibility free_only must match the scenario")
    return QualityScenario(
        scenario_id=_required_str(value, "id"),
        raw_text=_required_str(value, "raw_text"),
        membership=GroupCondition(str(value.get("membership", GroupCondition.UNKNOWN.value))),
        free_only=bool(value.get("free_only", False)),
        events=events,
        expected_outcome=_required_str(value, "expected_outcome"),
        expected_source_mutation_count=int(value["expected_source_mutation_count"]),
        expected_calendar_count=int(value["expected_calendar_count"]),
        expected_notification_kind=NotificationKind(
            _required_str(value, "expected_notification_kind")
        ),
        expected_visible_source_event_keys=visible_keys,
    )


def _event_from_json(value: dict[str, object]) -> QualityEvent:
    return QualityEvent(
        source=Source(_required_str(value, "source")),
        source_event_key=_required_str(value, "source_event_key"),
        title=_required_str(value, "title"),
        price_status=PriceStatus(_required_str(value, "price_status")),
        conflict_role=str(value["conflict_role"]) if "conflict_role" in value else None,
    )


def _required_str(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"g1 corpus requires non-empty string {key}")
    return result


def _required_list(value: Mapping[str, object], key: str) -> list[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValueError(f"g1 corpus requires list {key}")
    return result


def _quality_load_repeats() -> int:
    """Read the bounded opt-in repeat count for P5b; ordinary integration CI never sets it."""
    value = os.getenv(_QUALITY_LOAD_REPEATS_ENV)
    if value is None:
        pytest.skip(f"{_QUALITY_LOAD_REPEATS_ENV} is set only by `make quality-load`")
    try:
        repeats = int(value)
    except ValueError as error:
        raise ValueError(f"{_QUALITY_LOAD_REPEATS_ENV} must be an integer") from error
    if not 1 <= repeats <= _MAX_QUALITY_LOAD_REPEATS:
        raise ValueError(
            f"{_QUALITY_LOAD_REPEATS_ENV} must be between 1 and {_MAX_QUALITY_LOAD_REPEATS}"
        )
    return repeats


def _candidate_events(scenario: QualityScenario, scenario_index: int) -> list[CandidateEvent]:
    """Give each scenario a narrow future slot so the persistent catalog cannot affect ranking."""
    slot = _SCENARIO_START + timedelta(days=scenario_index * 2)
    return [
        CandidateEvent(
            source=event.source,
            source_event_id=event.source_event_key,
            title=event.title,
            start_at=slot + timedelta(hours=event_index * 4),
            end_at=slot + timedelta(hours=event_index * 4 + 2),
            registration_url=f"https://fixture.invalid/g1/{event.source_event_key}",
            city="San Francisco",
            description=f"synthetic quality fixture {scenario.scenario_id}",
            price_status=event.price_status,
        )
        for event_index, event in enumerate(scenario.events)
    ]


def _scenario_window(events: list[CandidateEvent], scenario_index: int) -> TimeWindow:
    slot = _SCENARIO_START + timedelta(days=scenario_index * 2)
    if not events:
        return TimeWindow(slot - timedelta(hours=1), slot + timedelta(hours=8))
    return TimeWindow(
        min(event.start_at for event in events) - timedelta(minutes=1),
        max(event.start_at for event in events) + timedelta(minutes=1),
    )


def _build_scenario_ports(
    scenario: QualityScenario, candidates: list[CandidateEvent]
) -> ScenarioPorts:
    """Construct only fixture-backed SourcePort implementations; no source can reach a network."""
    sources = {event.source for event in scenario.events}
    if len(sources) > 1:
        raise ValueError("each g1 scenario must use one source family")
    if not sources:
        return ScenarioPorts(None, {}, None, lambda: 0)
    source = next(iter(sources))
    if source is Source.PUBLIC_JSONLD:
        fixture_source = PublicJsonLdSource(
            user_agent="g1-quality-fixture",
            fixture_events=candidates,
            now=lambda: _FIXTURE_NOW,
        )
        return ScenarioPorts(fixture_source, {}, source, lambda: 0)
    if source is Source.MEETUP:
        overlay = scenario.scenario_id == _MEMBER_OVERLAY_ID
        fixture_source = ConfirmingSource(
            Source.MEETUP,
            fixture_events=candidates,
            membership_state=scenario.membership,
            pending_after_effect_once=overlay,
            event_log=[],
        )
        return ScenarioPorts(
            fixture_source,
            {Source.MEETUP: fixture_source},
            source,
            lambda: fixture_source.registration_effects,
            confirming_source=fixture_source,
            primary_source_event_id=(candidates[0].source_event_id if candidates else None),
        )
    if source is Source.LUMA:
        browser = ScriptedBrowserRsvpDriver(
            {
                candidate.source_event_id: ScriptedBrowserScenario(
                    initial_detection=BrowserRsvpObservation(
                        BrowserRsvpStatus.NOT_PRESENT, price_cents=0
                    ),
                    after_submit=BrowserRsvpObservation(BrowserRsvpStatus.CONFIRMED, price_cents=0),
                )
                for candidate in candidates
            }
        )
        fixture_source = LumaSource(browser, candidates)
        return ScenarioPorts(
            fixture_source,
            {Source.LUMA: fixture_source},
            source,
            lambda: browser.submit_calls,
        )
    raise ValueError(f"g1 corpus source {source.value} has no approved fixture adapter")


def _membership_resolver(scenario: QualityScenario) -> Callable[[Source], GroupCondition]:
    def resolve(source: Source) -> GroupCondition:
        return scenario.membership if source is Source.MEETUP else GroupCondition.UNKNOWN

    return resolve


def _fixture_discovery_policy_gate(
    source: SourcePort | None,
) -> StoreBackedDiscoveryPolicyGate:
    """Authorize only this scenario's offline discovery adapter.

    Production's durable policy deliberately keeps generic public/browser discovery disabled until
    an operator enables it. The quality harness supplies its own fixture-only adapter, so it must
    also supply the matching explicit fixture policy instead of relying on mutable database state.
    Unknown adapters remain denied by the real fail-closed gate used here.
    """
    policies: dict[Source, SourcePolicy] = {}
    if source is not None:
        capability = source.capability
        if capability.supports_api:
            modality = Modality.API
        elif capability.supports_browser_discovery:
            modality = Modality.BROWSER
        else:
            raise ValueError("g1 fixture source must declare a discovery modality")
        policies[capability.source] = SourcePolicy(
            source=capability.source,
            automation_allowed={modality: True},
        )
    return StoreBackedDiscoveryPolicyGate(MockDiscoveryPolicyReader(policies))


async def _tenant_outbox_records(tenant_id: UUID) -> list[OutboxRecord]:
    """Read just this scenario's committed rows; the global relay queue remains untouched."""
    async with tenant_session_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """SELECT id, tenant_id, topic, payload, attempt_count
                       FROM outbox WHERE tenant_id = :tenant_id ORDER BY id"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()
    records: list[OutboxRecord] = []
    for row in rows:
        payload_value = row.payload
        payload = (
            cast(dict[str, object], payload_value)
            if isinstance(payload_value, dict)
            else cast(dict[str, object], json.loads(str(payload_value)))
        )
        records.append(
            OutboxRecord(
                outbox_id=int(row.id),
                tenant_id=cast(UUID, row.tenant_id),
                topic=str(row.topic),
                payload=payload,
                attempt_count=int(row.attempt_count),
                lease_token=f"quality-first-{row.id}",
            )
        )
    return records


async def _tenant_action_audit_phase_counts(tenant_id: UUID) -> dict[str, int]:
    """Read only tenant-scoped P6a phase/count evidence, never audit identities or payloads."""
    async with tenant_session_scope(tenant_id) as session:
        rows = await session.execute(_ACTION_AUDIT_PHASE_COUNTS_QUERY, {"tenant_id": tenant_id})
    return {str(row.phase): int(row.audit_count) for row in rows}


async def _relay_tenant_records(records: list[OutboxRecord]) -> tuple[tuple[str, ...], int]:
    """Replay only copied tenant records so the real relay/rendering path proves visible dedup."""
    replay = [
        replace(record, lease_token=f"quality-replay-{index}")
        for index, record in enumerate(records)
    ]
    outbox = ScenarioOutbox([records, replay])
    notifier = MockNotifier()
    relay = OutboxRelay(outbox, notifier, DevelopmentNotificationSecretProtector())
    await relay.relay_once()
    await relay.relay_once()
    kinds = tuple(sorted(notification.kind.value for notification in notifier.sent))
    dedup_count = len({notification.dedup_key for notification in notifier.sent})
    if dedup_count != len(notifier.sent):
        raise AssertionError("quality relay emitted duplicate visible notifications")
    if len(outbox.notification_claims) != 2 * dedup_count:
        raise AssertionError("quality relay did not replay every visible tenant notification")
    return kinds, dedup_count


async def _wait_for_source_effect(source: ConfirmingSource) -> None:
    """Await the exact fixture effect before injecting its opaque confirmation (NFR-8, ADR-003)."""
    try:
        await asyncio.wait_for(
            source.wait_for_registration_effect(), timeout=_SOURCE_EFFECT_WAIT_SECONDS
        )
    except TimeoutError:
        pytest.fail(
            "g1 member Meetup workflow did not reach its pending source effect within "
            f"{_SOURCE_EFFECT_WAIT_SECONDS:.0f} seconds"
        )
    assert source.registration_effects == 1


async def _validate_feed(
    container: Container,
    scenario: QualityScenario,
    candidates: list[CandidateEvent],
    request: EventRequest,
    calendar: MockCalendar,
    tenant_id: UUID,
) -> None:
    """Assert fixture filtering/conflict semantics before the parent repeats the same feed read."""
    if any(event.conflict_role == "hard_conflict" for event in scenario.events):
        blocked_candidate = next(
            candidate
            for candidate, fixture in zip(candidates, scenario.events, strict=True)
            if fixture.conflict_role == "hard_conflict"
        )
        calendar.seed_busy(
            tenant_id,
            [
                BusyBlock(
                    blocked_candidate.start_at,
                    blocked_candidate.end_at or blocked_candidate.start_at,
                )
            ],
        )
    feed = await container.feed.build_feed(request, limit=10)
    candidate_keys = {candidate.source_event_id for candidate in candidates}
    visible_keys = {
        link.source_event_id
        for item in feed.items
        for link in item.canonical_event.source_links
        if link.source_event_id in candidate_keys
    }
    if scenario.expected_visible_source_event_keys is not None:
        assert visible_keys == set(scenario.expected_visible_source_event_keys)
    if any(event.conflict_role == "hard_conflict" for event in scenario.events):
        assert any(item.conflict_verdict is ConflictVerdict.BLOCKED for item in feed.items)
        assert any(item.conflict_verdict is not ConflictVerdict.BLOCKED for item in feed.items)


async def _execute_parent_workflow(
    container: Container,
    scenario: QualityScenario,
    ports: ScenarioPorts,
    tenant_id: UUID,
    request: EventRequest,
) -> tuple[RequestResult, bool]:
    """Run the real parent/child spine and, once, its lost-ACK/duplicate-start overlay."""
    workflow_id = request_workflow_id(tenant_id, request.request_id)
    queue = f"g1-quality-{scenario.scenario_id}-{uuid4().hex}"
    overlay = scenario.scenario_id == _MEMBER_OVERLAY_ID
    duplicate_parent_start_rejected = False
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=queue,
            workflows=[EventRequestWorkflow, RegistrationWorkflow],
            activities=_activities(),
        ),
    ):
        # The parent returns as soon as its child reports scheduled/handoff. Freeze automatic
        # time-skipping while it does so: otherwise the retained child's 2040 completion/TTL
        # timer can race this scenario's semantic assertions (ADR-003/007).
        with environment.auto_time_skipping_disabled():
            if not overlay:
                result = cast(
                    RequestResult,
                    await environment.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(tenant_id=str(tenant_id), request_id=str(request.request_id)),
                        id=workflow_id,
                        task_queue=queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    ),
                )
                return result, duplicate_parent_start_rejected

            handle = await environment.client.start_workflow(
                EventRequestWorkflow.run,
                RequestInput(tenant_id=str(tenant_id), request_id=str(request.request_id)),
                id=workflow_id,
                task_queue=queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            source = ports.confirming_source
            source_event_id = ports.primary_source_event_id
            if source is None or source_event_id is None:
                raise AssertionError("member Meetup overlay requires a confirming source")
            await _wait_for_source_effect(source)
            with pytest.raises(WorkflowAlreadyStartedError):
                await environment.client.start_workflow(
                    EventRequestWorkflow.run,
                    RequestInput(tenant_id=str(tenant_id), request_id=str(request.request_id)),
                    id=workflow_id,
                    task_queue=queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            duplicate_parent_start_rejected = True
            source.confirm_pending(tenant_id, source_event_id, Modality.API)
            canonical = _canonical_for_source_event(
                await container.catalog.retrieve(request.constraints, request.intent_embedding, 10),
                source_event_id,
            )
            child = environment.client.get_workflow_handle(
                registration_workflow_id(tenant_id, canonical.canonical_event_id)
            )
            await child.signal("confirmation_received", "g1-opaque-confirmation-reference")
            return cast(RequestResult, await handle.result()), duplicate_parent_start_rejected


async def _run_scenario(scenario: QualityScenario, scenario_index: int) -> ScenarioObservation:
    """Run one true intake→catalog→rank→workflow→calendar→notification scenario."""
    settings = get_settings()
    candidates = _candidate_events(scenario, scenario_index)
    window = _scenario_window(candidates, scenario_index)
    ports = _build_scenario_ports(scenario, candidates)
    calendar = MockCalendar()
    container = build_container(
        settings,
        discovery_sources=([ports.discovery_source] if ports.discovery_source is not None else []),
        register_sources=ports.register_sources,
        membership_resolver=_membership_resolver(scenario),
        calendar=calendar,
        discovery_policy_gate=_fixture_discovery_policy_gate(ports.discovery_source),
    )
    set_container(container)

    tenant_id = uuid4()
    tenant_tag = tenant_id.hex
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|g1-{scenario.scenario_id}-{tenant_tag}",
            f"g1-{tenant_tag}@example.test",
            f"g1-{tenant_tag}@u.fixture.test",
        )
    )
    await _seed_supported_registration_consents(tenant_id, ports.register_sources)
    parsed = await container.parser.parse(tenant_id, uuid4(), scenario.raw_text)
    assert parsed.constraints.budget_free is scenario.free_only
    request = EventRequest(
        request_id=parsed.request_id,
        tenant_id=parsed.tenant_id,
        raw_text=parsed.raw_text,
        constraints=RequestConstraints(
            time_window=window,
            categories=parsed.constraints.categories,
            budget_free=parsed.constraints.budget_free,
            hard_filters=parsed.constraints.hard_filters,
        ),
        intent_embedding=parsed.intent_embedding,
    )
    await container.discovery.discover(RequestConstraints(time_window=window))
    await container.request_repo.add(request)
    await _validate_feed(container, scenario, candidates, request, calendar, tenant_id)
    overlay = scenario.scenario_id == _MEMBER_OVERLAY_ID
    result, duplicate_parent_start_rejected = await _execute_parent_workflow(
        container, scenario, ports, tenant_id, request
    )

    action_audit_phases = await _tenant_action_audit_phase_counts(tenant_id)
    action_audit_count = sum(action_audit_phases.values())
    records = await _tenant_outbox_records(tenant_id)
    notification_kinds, notification_dedup_count = await _relay_tenant_records(records)
    assert result.outcome == scenario.expected_outcome
    assert ports.source_mutations() == scenario.expected_source_mutation_count
    assert len(calendar.entries(tenant_id)) == scenario.expected_calendar_count
    assert notification_kinds == (scenario.expected_notification_kind.value,)
    if scenario.expected_outcome == "registered":
        assert action_audit_count == 3
        assert action_audit_phases == _REGISTERED_AUDIT_PHASES
    if scenario.scenario_id == "g1-hard-conflict-backup-selected":
        source_ids = calendar.entries(tenant_id)[0].private_metadata[
            "events_concierge.source_event_ids"
        ]
        assert "meetup:g1-conflict-backup" in source_ids
        assert "meetup:g1-conflict-primary" not in source_ids
    if overlay:
        assert ports.source_mutations() == 1
        assert duplicate_parent_start_rejected

    return ScenarioObservation(
        tenant_id=tenant_id,
        request_id=request.request_id,
        scenario_id=scenario.scenario_id,
        outcome=result.outcome,
        selected_source=(
            ports.selected_source.value
            if result.outcome != "failed_no_candidate" and ports.selected_source is not None
            else None
        ),
        source_mutation_count=ports.source_mutations(),
        calendar_count=len(calendar.entries(tenant_id)),
        notification_kinds=notification_kinds,
        notification_dedup_count=notification_dedup_count,
        action_audit_count=action_audit_count,
        action_audit_phases=action_audit_phases,
        lost_source_ack_recovered=overlay and ports.source_mutations() == 1,
        duplicate_parent_start_rejected=duplicate_parent_start_rejected,
    )


def _canonical_for_source_event(
    candidates: Iterable[CanonicalEvent], source_event_id: str
) -> CanonicalEvent:
    for candidate in candidates:
        if any(link.source_event_id == source_event_id for link in candidate.source_links):
            return candidate
    raise AssertionError("quality fixture canonical event was not available to the child workflow")


def _report(corpus: QualityCorpus, observations: Iterable[ScenarioObservation]) -> QualityReport:
    observed = tuple(observations)
    outcomes = Counter(item.outcome for item in observed)
    selected_sources = Counter(
        item.selected_source for item in observed if item.selected_source is not None
    )
    notification_kinds = Counter(kind for item in observed for kind in item.notification_kinds)
    action_audit_phases: Counter[str] = Counter()
    for item in observed:
        action_audit_phases.update(item.action_audit_phases)
    return QualityReport(
        corpus_version=corpus.version,
        scenario_ids=tuple(item.scenario_id for item in observed),
        outcomes=dict(outcomes),
        selected_sources=dict(selected_sources),
        source_mutation_count=sum(item.source_mutation_count for item in observed),
        calendar_count=sum(item.calendar_count for item in observed),
        notification_kinds=dict(notification_kinds),
        notification_dedup_count=sum(item.notification_dedup_count for item in observed),
        action_audit_count=sum(item.action_audit_count for item in observed),
        action_audit_phases=dict(action_audit_phases),
        fault_overlays={
            "lost_source_ack": sum(item.lost_source_ack_recovered for item in observed),
            "duplicate_parent_start": sum(
                item.duplicate_parent_start_rejected for item in observed
            ),
        },
    )


async def _run_corpus(corpus: QualityCorpus) -> list[ScenarioObservation]:
    """Run the corpus serially: activity composition is process-global (ADR-003)."""
    return [
        await _run_scenario(scenario, scenario_index)
        for scenario_index, scenario in enumerate(corpus.scenarios)
    ]


async def _assert_request_is_tenant_isolated(
    owner: ScenarioObservation, other: ScenarioObservation
) -> None:
    """Prove the P5b tenant inputs reach the forced-RLS request boundary (FR-1.3/1.4, ADR-007)."""
    assert owner.tenant_id != other.tenant_id
    assert owner.request_id != other.request_id

    async with tenant_session_scope(owner.tenant_id) as session:
        owned = (
            await session.execute(_REQUEST_TENANT_QUERY, {"request_id": owner.request_id})
        ).scalar()
    assert owned == owner.tenant_id

    async with tenant_session_scope(other.tenant_id) as session:
        foreign = (
            await session.execute(_REQUEST_TENANT_QUERY, {"request_id": owner.request_id})
        ).scalar()
    assert foreign is None

    async with tenant_session_scope(None) as session:
        unset = (
            await session.execute(_REQUEST_TENANT_QUERY, {"request_id": owner.request_id})
        ).scalar()
    assert unset is None


def _scaled_expected_report(corpus: QualityCorpus, repeats: int) -> dict[str, object]:
    """Scale the P5a golden aggregate while preserving its intentionally sanitized shape."""
    expected = corpus.expected_report

    def scaled_counts(key: str) -> dict[str, int]:
        values = cast(dict[str, int], expected[key])
        return {name: count * repeats for name, count in values.items()}

    return {
        "corpus_version": expected["corpus_version"],
        "scenario_ids": list(cast(list[str], expected["scenario_ids"])) * repeats,
        "scenario_count": int(expected["scenario_count"]) * repeats,
        "outcomes": scaled_counts("outcomes"),
        "selected_sources": scaled_counts("selected_sources"),
        "source_mutation_count": int(expected["source_mutation_count"]) * repeats,
        "calendar_count": int(expected["calendar_count"]) * repeats,
        "notification_kinds": scaled_counts("notification_kinds"),
        "notification_dedup_count": int(expected["notification_dedup_count"]) * repeats,
        "action_audit_count": int(expected["action_audit_count"]) * repeats,
        "action_audit_phases": scaled_counts("action_audit_phases"),
        "fault_overlays": scaled_counts("fault_overlays"),
    }


def _assert_report_is_sanitized(report: Mapping[str, object], corpus: QualityCorpus) -> None:
    """Keep P5 reports useful for repeat comparisons without retaining fixture request content."""
    rendered = json.dumps(report, sort_keys=True)
    for scenario in corpus.scenarios:
        assert scenario.raw_text not in rendered
        for event in scenario.events:
            assert event.source_event_key not in rendered
            assert event.title not in rendered
    action_audit_phases = cast(dict[str, int], report["action_audit_phases"])
    assert set(action_audit_phases).issubset(_REGISTERED_AUDIT_PHASES)


async def test_g1_quality_harness_exercises_the_synthetic_workflow_matrix(db: None) -> None:
    """Eight offline journeys produce the versioned semantic golden report (P5a)."""
    corpus = _load_corpus()
    observations = await _run_corpus(corpus)
    report = _report(corpus, observations).normalized()

    assert report == corpus.expected_report
    _assert_report_is_sanitized(report, corpus)


@pytest.mark.quality_load
async def test_g1_quality_harness_repeats_the_corpus_with_isolated_tenants(db: None) -> None:
    """Prove bounded repeatability serially, without making a latency or capacity claim (P5b)."""
    repeats = _quality_load_repeats()
    corpus = _load_corpus()
    observations: list[ScenarioObservation] = []

    for _ in range(repeats):
        cycle = await _run_corpus(corpus)
        report = _report(corpus, cycle).normalized()
        assert report == corpus.expected_report
        _assert_report_is_sanitized(report, corpus)
        observations.extend(cycle)

    assert len(observations) == repeats * len(corpus.scenarios)
    assert len({item.tenant_id for item in observations}) == len(observations)
    await _assert_request_is_tenant_isolated(observations[0], observations[1])
    aggregate = _report(corpus, observations).normalized()
    assert aggregate == _scaled_expected_report(corpus, repeats)
    _assert_report_is_sanitized(aggregate, corpus)
