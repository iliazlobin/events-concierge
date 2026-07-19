"""The ADR-003 two-tier Temporal workflows.

The parent owns discovery/ranking and the bounded candidate attempt loop. The child owns one
``(tenant, canonical_event)`` saga, mints side-effect keys once in durable workflow state, and
coordinates narrow activities so retries converge rather than repeat a remote RSVP or calendar write.
After scheduling, that child remains the durable lifecycle owner for organizer-change and un-RSVP
signals rather than extending the short-lived parent (ADR-003/ADR-008).
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError

from ..domain import ids
from ..domain.enums import HandoffReason, HandoffReminderKind, Lane, LifecycleState, Source
from ..ports.sources import RegisterOutcome
from .dto import (
    AwaitConfirmationInput,
    AwaitConfirmationResult,
    CatalogRefreshActivityResult,
    CatalogRefreshInput,
    ChildOutcome,
    CloseFailedCandidateInput,
    CloseFailedCandidateResult,
    CompleteLifecycleInput,
    CompleteLifecycleResult,
    DedupeCalendarInput,
    DedupeCalendarResult,
    DiscoverResult,
    ExpireHandoffInput,
    ExpireHandoffResult,
    FinalizeNoCandidateInput,
    FinalizeNoCandidateResult,
    HandoffCompletionSignal,
    HandoffInput,
    HandoffReminderActivityResult,
    HandoffReminderInput,
    OrganizerChangeSignal,
    PendingLifecycleSignals,
    PolicyGateInput,
    PolicyGateResult,
    ReconcileOrganizerChangeInput,
    ReconcileOrganizerChangeResult,
    RegChildInput,
    RegChildResult,
    RegisterOrRsvpInput,
    RegisterOrRsvpResult,
    RegistrationSagaKeys,
    RequestInput,
    RequestResult,
    ResolveMembershipInput,
    ResolveMembershipResult,
    UnrsvpActivityResult,
    UnrsvpInput,
    UnrsvpSignal,
    WriteToCalendarInput,
    WriteToCalendarResult,
)

_ACTIVITY_TIMEOUT = timedelta(seconds=60)
_CONFIRMATION_TIMEOUT = timedelta(hours=24)
_DIRECTIVE_TIMEOUT = timedelta(minutes=10)
_SOURCE_RECOVERY_ATTEMPTS = 2
_HANDOFF_REMINDER_DB_RETRY_DELAY = timedelta(minutes=1)
_HANDOFF_COMPLETION_RETRY_DELAY = timedelta(minutes=1)
_HANDOFF_COMPLETION_RETRY_CAP = timedelta(hours=1)
_DIRECTIVE_CLOSE = "close"
_DIRECTIVE_DEMOTE_TO_HANDOFF = "demote_to_handoff"
_ORGANIZER_CHANGE_STATUSES = frozenset(("cancelled", "rescheduled"))


@workflow.defn
class CatalogRefreshWorkflow:
    """Resume P15a's one-GET LibCal refresh only through durable Pacer/lease-recovery timers.

    The guarded activity claims a short database lease, then either performs its single approved
    LibCal GET or releases that lease with a Pacer projection.  The workflow retains just the
    source/run identity, sleeps durably, and reclaims the same run key on wake.  It intentionally
    rejects paginated and redirecting modes until P15b persists cursor/staging state (FR-10.3/10.4,
    NFR-8, ADR-003/005).
    """

    @workflow.run
    async def run(self, inp: CatalogRefreshInput) -> CatalogRefreshActivityResult:
        """Retry a non-reserved Pacer defer or transient lease contention durably."""
        while True:
            result: CatalogRefreshActivityResult = await workflow.execute_activity(
                "refresh_catalog_single_get",
                inp,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=CatalogRefreshActivityResult,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if result.outcome == "skipped":
                # A stop before a successful run leaves its cadence slot due. Fail rather than
                # completing this deterministic workflow ID, so owner correction can restart the
                # same slot under ALLOW_DUPLICATE_FAILED_ONLY (NFR-8, ADR-003/004/005).
                raise ApplicationError("catalog refresh stopped before success")
            if result.outcome == "busy":
                # A second dispatcher or a just-expired activity can briefly own this exact
                # source/run lease. Ending successfully would consume the deterministic workflow
                # ID and prevent its later recovery under ALLOW_DUPLICATE_FAILED_ONLY, so retry
                # through a durable timer as the paged catalog spine does (ADR-003, NFR-8).
                await workflow.sleep(timedelta(seconds=1))
                continue
            if result.outcome != "deferred":
                return result
            retry_after_seconds = result.retry_after_seconds
            if retry_after_seconds is None or retry_after_seconds <= 0.0:
                raise ValueError("catalog Pacer defer requires a positive retry projection")
            await workflow.sleep(timedelta(seconds=retry_after_seconds))


@workflow.defn
class CatalogPagedRefreshWorkflow:
    """Resume a staged P15b/P15c/P15d/P15e Legistar refresh one page effect at a time.

    A full page is atomically staged and its lease is released before this workflow calls the next
    activity. A Pacer delay and a transient competing lease use workflow-owned timers. A terminal
    stage is promoted without a second GET, so a crash after stage commit cannot repeat a later
    source page or falsely mark a partial catalog success (FR-10.3/10.4, NFR-8, ADR-003/005).
    """

    @workflow.run
    async def run(self, inp: CatalogRefreshInput) -> CatalogRefreshActivityResult:
        """Drive the same opaque source/run identity until it reaches a terminal result."""
        while True:
            result: CatalogRefreshActivityResult = await workflow.execute_activity(
                "refresh_catalog_paged_legistar",
                inp,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=CatalogRefreshActivityResult,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if result.outcome == "progressed":
                continue
            if result.outcome == "busy":
                # A second dispatcher or an expired activity can briefly own the same durable run.
                # The cursor remains in PostgreSQL; wait rather than ending a successful workflow
                # that would reject a later duplicate start (ADR-003, NFR-8).
                await workflow.sleep(timedelta(seconds=1))
                continue
            if result.outcome == "skipped":
                # A source-policy/configuration stop has no successful run to advance the cadence
                # cursor. Fail the engine execution so the existing deterministic source/slot ID
                # can be reused after owner correction; returning it would permanently suppress
                # that retry under ALLOW_DUPLICATE_FAILED_ONLY (NFR-8, ADR-003/004/005).
                raise ApplicationError("paged catalog refresh stopped before success")
            if result.outcome != "deferred":
                return result
            retry_after_seconds = result.retry_after_seconds
            if retry_after_seconds is None or retry_after_seconds <= 0.0:
                raise ValueError("catalog Pacer defer requires a positive retry projection")
            await workflow.sleep(timedelta(seconds=retry_after_seconds))


@workflow.defn
class RegistrationWorkflow:
    """Child per ``(tenant, event)`` saga; workflow id is the engine-level idempotency key.

    Source mutation activities have exactly one Temporal attempt. A lost ACK is recovered by an
    explicit workflow loop that calls the read-before-mutate step again with the original key
    (NFR-8, ADR-003). After a successful calendar write, a parent-owned child remains signalable for
    its post-booking lifecycle (FR-8.7/8.8, ADR-008).
    """

    def __init__(self) -> None:
        self._confirmation_reference: str | None = None
        self._directive: str | None = None
        self._keys: RegistrationSagaKeys | None = None
        self._canonical_event_id: str | None = None
        # The detector can legitimately signal after the REGISTERED transition but before the
        # calendar-write activity returns. Those signals must survive that interval, while user
        # un-RSVP remains post-booking-only.
        self._organizer_change_buffer_open = False
        self._post_booking_lifecycle_active = False
        self._completion_deadline_at: str | None = None
        self._pending_organizer_changes: dict[str, OrganizerChangeSignal] = {}
        self._handled_organizer_change_fingerprints: set[str] = set()
        self._organizer_change_serial_dedup_enabled = False
        self._pending_unrsvp: dict[str, UnrsvpSignal] = {}
        self._handoff_completion_buffer_open = False
        self._pending_handoff_completions: dict[str, HandoffCompletionSignal] = {}
        self._handled_handoff_completion_ids: set[str] = set()

    @workflow.signal
    def confirmation_received(self, confirmation_reference: str) -> None:
        """Receive an opaque confirmation reference; plaintext codes never enter workflow history."""
        if confirmation_reference:
            self._confirmation_reference = confirmation_reference

    @workflow.signal
    def directive(self, directive: str) -> None:
        """Accept the parent-owned fall-through or terminal-handoff directive (ADR-003)."""
        if directive in (_DIRECTIVE_CLOSE, _DIRECTIVE_DEMOTE_TO_HANDOFF):
            self._directive = directive

    @workflow.signal
    def organizer_change(self, signal: OrganizerChangeSignal) -> None:
        """Queue one detector-normalized cancel/reschedule signal by its stable fingerprint.

        The central detector may redeliver after a fanout crash.  Keeping the first payload per
        fingerprint makes that at-least-once delivery converge before the eventual reconcile
        activities execute it (FR-8.7/8.7a, ADR-008).
        """
        if not self._organizer_change_buffer_open:
            return
        if (
            not signal.fingerprint
            or not signal.canonical_event_id
            or signal.source not in {source.value for source in Source}
            or signal.event_status not in _ORGANIZER_CHANGE_STATUSES
        ):
            return
        if (
            self._canonical_event_id is not None
            and signal.canonical_event_id != self._canonical_event_id
        ):
            return
        if (
            self._organizer_change_serial_dedup_enabled
            and signal.fingerprint in self._handled_organizer_change_fingerprints
        ):
            return
        self._pending_organizer_changes.setdefault(signal.fingerprint, signal)

    @workflow.signal
    def unrsvp_requested(self, signal: UnrsvpSignal) -> None:
        """Queue a user withdrawal once by inbound request id (FR-6.7/8.8, ADR-003)."""
        if self._post_booking_lifecycle_active and signal.request_id:
            self._pending_unrsvp.setdefault(signal.request_id, signal)

    @workflow.signal
    def handoff_completed(self, signal: HandoffCompletionSignal) -> None:
        """Queue a capability-authenticated mark-done once by its stable command identity."""
        if (
            self._handoff_completion_buffer_open
            and signal.task_id
            and signal.completion_id
            and signal.evidence == "user_mark_done"
            and signal.completion_id not in self._handled_handoff_completion_ids
        ):
            self._pending_handoff_completions.setdefault(signal.completion_id, signal)

    @workflow.query
    def pending_lifecycle_signals(self) -> PendingLifecycleSignals:
        """Expose the durable command queues without consuming or mutating them.

        The query is intentionally diagnostic-only: a caller cannot race the workflow's future
        journaled reconcile/withdraw activities by reading then acknowledging a signal itself.
        """
        return PendingLifecycleSignals(
            organizer_changes=list(self._pending_organizer_changes.values()),
            unrsvp_requests=list(self._pending_unrsvp.values()),
            handoff_completions=list(self._pending_handoff_completions.values()),
        )

    @workflow.run
    async def run(self, inp: RegChildInput) -> RegChildResult:
        self._canonical_event_id = inp.canonical_event_id
        # A handled-fingerprint set would otherwise change the command sequence of a retained
        # pre-P37 history that replayed a serial duplicate.  New retained children record this
        # marker before they can accept detector traffic; old histories preserve their original
        # activity sequence (ADR-003/ADR-008).
        if inp.keep_open_after_scheduling:
            self._organizer_change_serial_dedup_enabled = workflow.patched(
                "p37-organizer-change-serial-dedup-v1"
            )
        self._keys = self._mint_keys()
        keys = self._keys
        resolution = await self._resolve_membership_with_pacing(inp, keys)
        handoff_eligible = Lane.HANDOFF.value in resolution.lane_plan
        initial_result = await self._initial_resolution_result(
            inp, keys, resolution, handoff_eligible
        )
        if initial_result is not None:
            return initial_result

        for lane_value in resolution.lane_plan:
            lane = Lane(lane_value)
            if lane is Lane.HANDOFF:
                break
            source_key = keys.source_by_lane.get(lane.value)
            if source_key is None:
                raise RuntimeError("registration workflow is missing its minted source key")
            gate: PolicyGateResult = await workflow.execute_activity(
                "policy_gate",
                PolicyGateInput(
                    tenant_id=inp.tenant_id,
                    canonical_event_id=inp.canonical_event_id,
                    lane=lane.value,
                    workflow_id=keys.workflow_id,
                    source_idempotency_key=source_key,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=PolicyGateResult,
            )
            if not gate.allowed:
                if gate.source_quarantined:
                    return await self._finish(
                        inp,
                        await self._handoff(
                            inp,
                            keys,
                            gate.detail or "source quarantined",
                        ),
                    )
                continue

            source = await self._register_with_recovery(inp, keys, lane)
            if source is None:
                continue
            if source.pacing_status == "degrade":
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        f"Pacer degrade: {source.detail}",
                        reason=HandoffReason.SATURATION,
                    ),
                )
            if self._browser_saturation(source.pacing_status, lane):
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        f"Browser admission saturated: {source.detail}",
                        reason=HandoffReason.SATURATION,
                    ),
                )
            if source.outcome == RegisterOutcome.SOURCE_QUARANTINED.value:
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        source.detail or "source quarantined",
                    ),
                )
            confirmation = await self._record_confirmation(inp, keys, lane, source)
            if confirmation is None:
                continue
            if confirmation.pacing_status == "degrade":
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        f"Pacer degrade: {confirmation.detail}",
                        reason=HandoffReason.SATURATION,
                    ),
                )
            if self._browser_saturation(confirmation.pacing_status, lane):
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        f"Browser admission saturated: {confirmation.detail}",
                        reason=HandoffReason.SATURATION,
                    ),
                )
            if confirmation.status == "quarantined":
                return await self._finish(
                    inp,
                    await self._handoff(
                        inp,
                        keys,
                        confirmation.detail or "source quarantined",
                    ),
                )
            while confirmation.status == "pending":
                try:
                    await workflow.wait_condition(
                        lambda: self._confirmation_reference is not None,
                        timeout=_CONFIRMATION_TIMEOUT,
                        timeout_summary="registration-confirmation",
                    )
                except TimeoutError:
                    return await self._fail_or_wait_for_directive(
                        inp,
                        keys,
                        "confirmation timed out",
                        handoff_eligible=handoff_eligible,
                    )
                confirmation_reference = self._confirmation_reference
                self._confirmation_reference = None
                confirmation = await self._record_confirmation(
                    inp,
                    keys,
                    lane,
                    source,
                    confirmation_reference=confirmation_reference,
                )
                if confirmation is not None and confirmation.pacing_status == "degrade":
                    return await self._finish(
                        inp,
                        await self._handoff(
                            inp,
                            keys,
                            f"Pacer degrade: {confirmation.detail}",
                            reason=HandoffReason.SATURATION,
                        ),
                    )
                if confirmation is not None and self._browser_saturation(
                    confirmation.pacing_status, lane
                ):
                    return await self._finish(
                        inp,
                        await self._handoff(
                            inp,
                            keys,
                            f"Browser admission saturated: {confirmation.detail}",
                            reason=HandoffReason.SATURATION,
                        ),
                    )
                if confirmation is None:
                    return await self._fail_or_wait_for_directive(
                        inp,
                        keys,
                        "confirmation signal could not be applied",
                        handoff_eligible=handoff_eligible,
                    )
                if confirmation.status == "quarantined":
                    return await self._finish(
                        inp,
                        await self._handoff(
                            inp,
                            keys,
                            confirmation.detail or "source quarantined",
                        ),
                    )
            if confirmation.status == "confirmed":
                self._open_organizer_change_buffer(inp)
                self._activate_post_booking_lifecycle(inp)
                calendar_result, scheduled = await self._calendar_stage(inp, keys, lane.value)
                return await self._finish(
                    inp,
                    calendar_result,
                    remain_open_after_scheduling=scheduled,
                )

        return await self._fail_or_wait_for_directive(
            inp,
            keys,
            "all autonomous lanes exhausted",
            handoff_eligible=handoff_eligible,
        )

    def _mint_keys(self) -> RegistrationSagaKeys:
        """Mint all side-effect IDs once from deterministic workflow state (FR-8.3, ADR-003)."""
        workflow_id = workflow.info().workflow_id
        run_id = workflow.info().run_id
        prefix = f"{workflow_id}:{run_id}"
        return RegistrationSagaKeys(
            workflow_id=workflow_id,
            run_id=run_id,
            membership_queue_item_id=f"{prefix}:membership_read:1",
            source_by_lane={
                Lane.AUTONOMOUS_SLA.value: f"{prefix}:register_or_rsvp:1",
                Lane.BROWSER_BEST_EFFORT.value: f"{prefix}:register_or_rsvp:2",
            },
            registration_read_by_lane={
                Lane.AUTONOMOUS_SLA.value: f"{prefix}:registration_read:1",
                Lane.BROWSER_BEST_EFFORT.value: f"{prefix}:registration_read:2",
            },
            awaiting_transition_id=f"{prefix}:awaiting_confirmation:1",
            confirmation_read_queue_item_id=f"{prefix}:confirmation_read:1",
            registered_transition_id=f"{prefix}:registered:1",
            scheduled_transition_id=f"{prefix}:scheduled:1",
            completed_transition_id=f"{prefix}:completed:1",
            close_candidate_transition_id=f"{prefix}:candidate-close:1",
            handoff_task_id=f"{workflow_id}:handoff",
            handoff_transition_id=f"{prefix}:handoff:1",
            handoff_expiry_transition_id=f"{prefix}:handoff-expired:1",
            calendar_recovery_task_id=f"{workflow_id}:calendar-recovery",
            calendar_recovery_expiry_transition_id=f"{prefix}:calendar-recovery-expired:1",
        )

    async def _initial_resolution_result(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        resolution: ResolveMembershipResult,
        handoff_eligible: bool,
    ) -> RegChildResult | None:
        """Resolve every pre-lane terminal outcome before any source dispatch.

        This keeps the workflow run method to the durable lane loop itself. In particular, a
        membership-source ban/403 has already tripped policy when it reaches the ``quarantined``
        status, so it becomes a direct human handoff rather than a parent fall-through or a
        second source call (FR-10.3, AC-72, ADR-003).
        """
        if resolution.pacing_status == "degrade":
            return await self._finish(
                inp,
                await self._handoff(
                    inp,
                    keys,
                    f"Pacer degrade: {resolution.detail}",
                    reason=HandoffReason.SATURATION,
                ),
            )
        if resolution.pacing_status is not None:
            return await self._fail_or_wait_for_directive(
                inp,
                keys,
                f"Pacer {resolution.pacing_status}: {resolution.detail}",
                handoff_eligible=handoff_eligible,
            )
        if resolution.status == "scheduled":
            self._open_organizer_change_buffer(inp)
            self._activate_post_booking_lifecycle(inp)
            calendar_result, scheduled = await self._calendar_stage(inp, keys, resolution.lane)
            return await self._finish(
                inp,
                RegChildResult(
                    status=calendar_result.status,
                    lane=calendar_result.lane,
                    calendar_event_id=calendar_result.calendar_event_id,
                    handoff_task_id=calendar_result.handoff_task_id,
                    handoff_expires_at=calendar_result.handoff_expires_at,
                    handoff_expiry_transition_id=calendar_result.handoff_expiry_transition_id,
                    handoff_created_at=calendar_result.handoff_created_at,
                    detail=resolution.detail or calendar_result.detail,
                ),
                remain_open_after_scheduling=scheduled,
            )
        if resolution.status == "registered":
            self._open_organizer_change_buffer(inp)
            self._activate_post_booking_lifecycle(inp)
            calendar_result, scheduled = await self._calendar_stage(inp, keys, resolution.lane)
            return await self._finish(
                inp,
                calendar_result,
                remain_open_after_scheduling=scheduled,
            )
        if resolution.status == "quarantined":
            return await self._finish(
                inp,
                await self._handoff(
                    inp,
                    keys,
                    resolution.detail or "source quarantined",
                ),
            )
        if resolution.status == "handoff":
            return await self._finish(
                inp,
                await self._handoff(inp, keys, resolution.detail),
            )
        if resolution.status != "ready":
            return await self._fail_or_wait_for_directive(
                inp, keys, resolution.detail, handoff_eligible=handoff_eligible
            )
        return None

    def _open_organizer_change_buffer(self, inp: RegChildInput) -> None:
        """Accept detector traffic once a long-lived child has durably reached ``REGISTERED``."""
        if inp.keep_open_after_scheduling:
            self._organizer_change_buffer_open = True

    def _activate_post_booking_lifecycle(self, inp: RegChildInput) -> None:
        """Accept user withdrawal after factual registration, including calendar recovery (FR-8.8).

        A failed calendar write leaves the source RSVP factual in ``REGISTERED``.  It is therefore
        already a post-booking lifecycle even though its manual calendar-recovery task has not yet
        produced a scheduled entry (ADR-007).
        """
        if inp.keep_open_after_scheduling:
            self._post_booking_lifecycle_active = True

    async def _resolve_membership_with_pacing(
        self, inp: RegChildInput, keys: RegistrationSagaKeys
    ) -> ResolveMembershipResult:
        """Repeat a pre-effect membership read only after a durable Pacer timer (ADR-005)."""
        while True:
            resolution: ResolveMembershipResult = await workflow.execute_activity(
                "resolve_membership",
                ResolveMembershipInput(
                    tenant_id=inp.tenant_id,
                    canonical_event_id=inp.canonical_event_id,
                    workflow_id=keys.workflow_id,
                    pacer_queue_item_id=keys.membership_queue_item_id,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=ResolveMembershipResult,
            )
            if resolution.pacing_status != "wait":
                return resolution
            await self._pacer_backoff(resolution.retry_after_seconds)

    async def _register_with_recovery(
        self, inp: RegChildInput, keys: RegistrationSagaKeys, lane: Lane
    ) -> RegisterOrRsvpResult | None:
        """Retry only through a fresh remote state read, never through a blind source retry."""
        source_key = keys.source_by_lane.get(lane.value)
        registration_read_queue_item_id = keys.registration_read_by_lane.get(lane.value)
        if source_key is None or registration_read_queue_item_id is None:
            return None
        source_attempt = 0
        while source_attempt < _SOURCE_RECOVERY_ATTEMPTS:
            try:
                result = await workflow.execute_activity(
                    "register_or_rsvp",
                    RegisterOrRsvpInput(
                        tenant_id=inp.tenant_id,
                        canonical_event_id=inp.canonical_event_id,
                        lane=lane.value,
                        workflow_id=keys.workflow_id,
                        source_idempotency_key=source_key,
                        registration_read_queue_item_id=registration_read_queue_item_id,
                    ),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    result_type=RegisterOrRsvpResult,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                source_attempt += 1
                if source_attempt == _SOURCE_RECOVERY_ATTEMPTS:
                    return None
                continue
            result = cast(RegisterOrRsvpResult, result)
            if result.pacing_status == "wait":
                await self._pacer_backoff(result.retry_after_seconds)
                continue
            if result.pacing_status == "degrade":
                return result
            if self._browser_saturation(result.pacing_status, lane):
                return result
            if result.pacing_status is not None:
                return None
            return result
        return None

    async def _record_confirmation(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        lane: Lane,
        source: RegisterOrRsvpResult,
        *,
        confirmation_reference: str | None = None,
    ) -> AwaitConfirmationResult | None:
        """Journal confirmation state after only a confirmed/pending source result."""
        outcome = RegisterOutcome(source.outcome)
        if outcome not in (
            RegisterOutcome.CONFIRMED,
            RegisterOutcome.NO_OP_ALREADY_CONFIRMED,
            RegisterOutcome.PENDING_CONFIRMATION,
        ):
            return None
        # ``await_confirmation`` can commit REGISTERED before its activity-completion event reaches
        # this workflow. Open the detector buffer first so an outbox/projector fanout in that tiny
        # interval is journaled here, but it still cannot drain until a successful calendar write.
        self._open_organizer_change_buffer(inp)
        while True:
            result = await workflow.execute_activity(
                "await_confirmation",
                AwaitConfirmationInput(
                    tenant_id=inp.tenant_id,
                    canonical_event_id=inp.canonical_event_id,
                    workflow_id=keys.workflow_id,
                    lane=lane.value,
                    source_outcome=outcome.value,
                    awaiting_transition_id=keys.awaiting_transition_id,
                    registered_transition_id=keys.registered_transition_id,
                    confirmation_read_queue_item_id=keys.confirmation_read_queue_item_id,
                    confirmation_reference=confirmation_reference,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=AwaitConfirmationResult,
            )
            result = cast(AwaitConfirmationResult, result)
            if result.pacing_status == "wait":
                await self._pacer_backoff(result.retry_after_seconds)
                continue
            if result.pacing_status == "degrade":
                return result
            if self._browser_saturation(result.pacing_status, lane):
                return result
            if result.pacing_status is not None:
                return None
            return result

    @staticmethod
    def _browser_saturation(pacing_status: str | None, lane: Lane) -> bool:
        """Treat a full browser pool as terminal, without changing non-browser Pacer behavior.

        P1d deliberately leaves generic ``SATURATED`` decisions available for future source
        policies.  P1e scopes AC-45's immediate handoff to Luma's best-effort browser lane.
        """
        return pacing_status == "saturated" and lane is Lane.BROWSER_BEST_EFFORT

    async def _pacer_backoff(self, retry_after_seconds: float | None) -> None:
        """Yield a durable Temporal timer; projected Pacer waits never occupy activity workers."""
        await workflow.sleep(timedelta(seconds=max(retry_after_seconds or 0.0, 0.001)))

    async def _calendar_stage(
        self, inp: RegChildInput, keys: RegistrationSagaKeys, lane: str | None
    ) -> tuple[RegChildResult, bool]:
        """Run deterministic dedupe/write and forward-compensate only after exhausted retries."""
        calendar_id = ids.calendar_event_id(UUID(inp.tenant_id), UUID(inp.canonical_event_id))
        dedupe: DedupeCalendarResult = await workflow.execute_activity(
            "dedupe_calendar",
            DedupeCalendarInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                calendar_event_id=calendar_id,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=DedupeCalendarResult,
        )
        try:
            written: WriteToCalendarResult = await workflow.execute_activity(
                "write_to_calendar",
                WriteToCalendarInput(
                    tenant_id=inp.tenant_id,
                    canonical_event_id=inp.canonical_event_id,
                    workflow_id=keys.workflow_id,
                    calendar_event_id=dedupe.calendar_event_id,
                    scheduled_transition_id=keys.scheduled_transition_id,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                result_type=WriteToCalendarResult,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            return await self._calendar_recovery(inp, keys, str(exc)), False
        if inp.keep_open_after_scheduling:
            self._post_booking_lifecycle_active = True
            self._completion_deadline_at = written.completion_deadline_at
        return (
            RegChildResult(
                status="registered",
                lane=lane,
                calendar_event_id=written.calendar_event_id,
            ),
            True,
        )

    async def _handoff(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        detail: str,
        *,
        reason: HandoffReason = HandoffReason.DEFERRED_REGISTER,
    ) -> RegChildResult:
        """Compensate a terminal child outcome with a once-minted task id (ADR-003/005)."""
        # Open the signal buffer before the task-creation activity. Once that transaction commits,
        # the capability endpoint may immediately signal this workflow; an activity acknowledgement
        # must not be a prerequisite for preserving the durable command (FR-6.3, NFR-8).
        if inp.keep_open_after_scheduling:
            self._handoff_completion_buffer_open = True
        result = await workflow.execute_activity(
            "route_to_handoff",
            HandoffInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=keys.workflow_id,
                handoff_task_id=keys.handoff_task_id,
                handoff_transition_id=keys.handoff_transition_id,
                handoff_expiry_transition_id=keys.handoff_expiry_transition_id,
                detail=detail,
                reason=reason.value,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=RegChildResult,
        )
        return cast(RegChildResult, result)

    async def _calendar_recovery(
        self, inp: RegChildInput, keys: RegistrationSagaKeys, detail: str
    ) -> RegChildResult:
        """Forward-recover a confirmed RSVP whose calendar write exhausted idempotent retries."""
        result = await workflow.execute_activity(
            "compensate_calendar_write",
            HandoffInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=keys.workflow_id,
                handoff_task_id=keys.calendar_recovery_task_id,
                handoff_transition_id=keys.handoff_transition_id,
                handoff_expiry_transition_id=keys.calendar_recovery_expiry_transition_id,
                detail=detail,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=RegChildResult,
        )
        return cast(RegChildResult, result)

    async def _fail_or_wait_for_directive(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        detail: str,
        *,
        handoff_eligible: bool = False,
    ) -> RegChildResult:
        """Report a failed candidate then obey the parent-owned terminal-handoff choice.

        The direct invocation path retained for focused child tests has no parent. It preserves the
        safe fallback by creating the handoff itself. Real request children instead park on a
        durable ten-minute directive wait so the parent can consume a candidate attempt and either
        fall through or convert this exact child into the terminal handoff (ADR-003, AC-32).
        """
        if workflow.info().parent is None:
            return await self._finish(inp, await self._handoff(inp, keys, detail))

        await self._report_to_parent(
            ChildOutcome(
                canonical_event_id=inp.canonical_event_id,
                status="failed",
                detail=detail,
                handoff_eligible=handoff_eligible,
            ),
        )
        try:
            await workflow.wait_condition(
                lambda: self._directive is not None,
                timeout=_DIRECTIVE_TIMEOUT,
                timeout_summary="registration-directive",
            )
        except TimeoutError:
            if not workflow.patched("p3c-directive-terminality-v1"):
                return RegChildResult(status="failed", detail=f"{detail}; directive timed out")
            return await self._finish(
                inp,
                await self._close_failed_candidate(
                    inp, keys, f"{detail}; directive timed out", "directive_timeout"
                ),
            )
        if self._directive == _DIRECTIVE_DEMOTE_TO_HANDOFF:
            return await self._finish(inp, await self._handoff(inp, keys, detail))
        if not workflow.patched("p3c-directive-terminality-v1"):
            return RegChildResult(status="failed", detail=detail)
        return await self._finish(
            inp,
            await self._close_failed_candidate(inp, keys, detail, "parent_fallthrough"),
        )

    async def _close_failed_candidate(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        detail: str,
        reason: str,
    ) -> RegChildResult:
        """Release a declined child without leaving its non-terminal lifecycle orphaned.

        Its caller has recorded the directive-terminality patch marker before adding this guarded
        activity, preserving deterministic replay while enforcing ADR-007's
        closed-workflow/terminal-lifecycle invariant going forward (ADR-003/007).
        """
        result: CloseFailedCandidateResult = await workflow.execute_activity(
            "close_failed_candidate",
            CloseFailedCandidateInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=keys.workflow_id,
                transition_id=keys.close_candidate_transition_id,
                reason=reason,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=CloseFailedCandidateResult,
        )
        terminal_state = result.terminal_state
        if terminal_state is None:
            raise RuntimeError(f"failed candidate could not terminalize lifecycle: {result.detail}")
        return RegChildResult(status=terminal_state, detail=detail)

    async def _finish(
        self,
        inp: RegChildInput,
        result: RegChildResult,
        *,
        remain_open_after_scheduling: bool = False,
    ) -> RegChildResult:
        """Report an outcome, then retain a scheduled or task-owning child for its lifecycle.

        A parent request is deliberately short-lived.  Once scheduling succeeds it can return the
        user-visible registration result, while this ``(tenant, event)`` workflow remains open to
        accept deduplicated organizer-change and un-RSVP signals.  Standalone child executions keep
        their focused-test behavior and return normally (FR-8.1/8.7/8.8, ADR-003/ADR-008).
        """
        await self._report_to_parent(
            ChildOutcome(
                canonical_event_id=inp.canonical_event_id,
                status=result.status,
                lane=result.lane,
                detail=result.detail or "",
            ),
        )
        if inp.keep_open_after_scheduling:
            if self._keys is None:
                raise RuntimeError("retained lifecycle requires workflow-minted saga keys")
            if remain_open_after_scheduling:
                lifecycle_result = await self._wait_for_lifecycle_work(inp, self._keys)
                if lifecycle_result is not None:
                    return lifecycle_result
            if result.handoff_task_id is not None:
                return await self._wait_for_handoff_task(inp, result)
        return result

    async def _wait_for_handoff_task(
        self, inp: RegChildInput, result: RegChildResult
    ) -> RegChildResult:
        """Own the durable task-TTL timer after reporting a first-class handoff outcome.

        The database activity returned the persisted absolute expiry and the once-minted guarded
        transition id.  A short-lived parent has already received the user-visible outcome, while
        this child remains the sole authority allowed to turn the ignored task into ``expired``.
        Bare mark-done completion deliberately remains deferred behind FR-16/D6 verification.
        """
        # Retained executions that predate P3c only journaled the TTL timer.  Preserve that
        # exact command history while new children let factual post-booking signals interrupt a
        # calendar-recovery or withdrawal task (FR-8.7/8.8, ADR-003/007).
        if not workflow.patched("p3c-handoff-signal-races-v1"):
            return await self._expire_handoff_task(inp, result)
        # Preserve the P3c signal-race command sequence for histories that began before reminder
        # timers existed. New children record a second marker and own both cadence timers here.
        if not workflow.patched("p3c-handoff-reminders-v1"):
            return await self._wait_for_handoff_task_with_signals(inp, result)
        return await self._wait_for_handoff_task_with_signals_and_reminders(inp, result)

    async def _wait_for_handoff_task_with_signals(
        self, inp: RegChildInput, initial: RegChildResult
    ) -> RegChildResult:
        """Give queued post-booking signals priority over an active handoff TTL.

        A calendar-recovery task exists while the RSVP is factual ``REGISTERED``.  An organizer
        cancellation/reschedule or user un-RSVP therefore remains a real lifecycle command, not
        an instruction that should wait for a manual task to time out.  The task guard and the
        lifecycle transition are still the database authority: a recovery transition cancels the
        obsolete calendar task atomically, and a withdrawal handoff supersedes it under the
        existing guarded creator (FR-6.6/8.7/8.8, ADR-003/007/008).
        """
        result = initial
        while True:
            task_id = result.handoff_task_id
            expiry = result.handoff_expires_at
            if task_id is None or expiry is None:
                raise RuntimeError("a retained handoff requires persisted task expiry context")
            deadline_at = self._parse_completion_deadline(expiry)
            if (
                deadline_at <= workflow.now()
                and not self._pending_unrsvp
                and not self._pending_organizer_changes
            ):
                return await self._expire_handoff_task(inp, result)

            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending_unrsvp) or bool(self._pending_organizer_changes),
                    timeout=max(deadline_at - workflow.now(), timedelta(0)),
                    timeout_summary="handoff-task-signal-or-expiry",
                )
            except TimeoutError:
                # A signal received at the same durable timestamp wins over expiry if it was
                # journaled in the workflow state before this loop decides to terminalize.
                continue

            # User intent wins when both command kinds are waiting, matching the scheduled
            # lifecycle path: remove the calendar intent and safely withdraw before a later
            # organizer reschedule can write a new calendar entry.
            if self._pending_unrsvp:
                request_id = next(iter(self._pending_unrsvp))
                if self._keys is None:
                    raise RuntimeError("retained handoff requires workflow-minted saga keys")
                unrsvp_result = await self._unrsvp_with_recovery(inp, self._keys, request_id)
                if unrsvp_result.status == "pacing_wait":
                    await self._pacer_backoff(unrsvp_result.retry_after_seconds)
                    continue
                self._pending_unrsvp.pop(request_id, None)
                if unrsvp_result.status == "cancelled":
                    return RegChildResult(status="cancelled", detail=unrsvp_result.detail)
                if unrsvp_result.status == "handoff":
                    result = RegChildResult(
                        status="handoff",
                        handoff_task_id=unrsvp_result.handoff_task_id,
                        handoff_expires_at=unrsvp_result.handoff_expires_at,
                        handoff_expiry_transition_id=unrsvp_result.handoff_expiry_transition_id,
                        detail=unrsvp_result.detail,
                    )
                continue

            fingerprint = next(iter(self._pending_organizer_changes))
            organizer_signal = self._pending_organizer_changes[fingerprint]
            if self._keys is None:
                raise RuntimeError("retained handoff requires workflow-minted saga keys")
            reconcile_result = await self._reconcile_with_recovery(
                inp, self._keys, organizer_signal
            )
            self._pending_organizer_changes.pop(fingerprint, None)
            if reconcile_result.status == "cancelled":
                return RegChildResult(status="cancelled", detail=reconcile_result.detail)
            if reconcile_result.status == "reconciled":
                if reconcile_result.completion_deadline_at is None:
                    raise RuntimeError(
                        "reconciled organizer change did not return a completion deadline"
                    )
                self._completion_deadline_at = reconcile_result.completion_deadline_at
                lifecycle_result = await self._wait_for_lifecycle_work(inp, self._keys)
                if lifecycle_result is None:
                    raise RuntimeError("reconciled lifecycle ended without a terminal child result")
                return lifecycle_result

    async def _wait_for_handoff_task_with_signals_and_reminders(
        self, inp: RegChildInput, initial: RegChildResult
    ) -> RegChildResult:
        """Own task reminders without letting them outrank durable user or organizer commands.

        A retained child owns Temporal timers only; it never decides that a reminder is valid from
        its own state.  Each timer calls the guarded activity with a stable task/cadence identity,
        and PostgreSQL verifies persisted creation time, task activity, and TTL before atomically
        adding the reminder ledger/outbox effect.  This loop deliberately keeps the pre-reminder
        signal implementation separate for replay compatibility (FR-6.6, FR-8.3/8.7/8.8,
        ADR-003/007/009).
        """
        # Preserve pre-completion task histories while new/continued children record the marker
        # before changing their timer wait predicates or activity sequence.
        completion_enabled = workflow.patched("p4-secure-handoff-completion-v1")
        # Existing retained histories used Temporal's default unlimited activity retry. Preserve
        # their command sequence while new histories bound each dependency probe to one attempt
        # and move retries into this workflow's durable, interruptible timer loop.
        bounded_completion_retry = workflow.patched(
            "p44-bounded-handoff-completion-retry-v1"
        )
        result = initial
        active_task_id: str | None = None
        sent_reminders: set[HandoffReminderKind] = set()
        retry_not_before: dict[HandoffReminderKind, datetime] = {}
        completion_retry_not_before: dict[str, datetime] = {}
        completion_retry_counts: dict[str, int] = {}
        committed_completion_ids: set[str] = set()
        while True:
            task_id = result.handoff_task_id
            expiry = result.handoff_expires_at
            expiry_transition_id = result.handoff_expiry_transition_id
            created_at = result.handoff_created_at
            if (
                task_id is None
                or expiry is None
                or expiry_transition_id is None
                or created_at is None
            ):
                raise RuntimeError(
                    "a retained handoff reminder requires persisted task timing context"
                )
            if task_id != active_task_id:
                # A user withdrawal can create a distinct replacement task. Its cadence begins
                # from that task's database-created instant, never from the obsolete recovery task.
                active_task_id = task_id
                self._reset_handoff_task_retry_state(
                    sent_reminders,
                    retry_not_before,
                    completion_retry_not_before,
                    completion_retry_counts,
                    committed_completion_ids,
                )

            deadline_at = self._parse_completion_deadline(expiry)
            created_at_value = self._parse_completion_deadline(created_at)
            expiry_settled, expiry_result = await self._maybe_settle_handoff_expiry(
                inp,
                result,
                task_id,
                deadline_at,
                completion_enabled=completion_enabled,
                bounded_retry=bounded_completion_retry,
                completion_retry_not_before=completion_retry_not_before,
                committed_completion_ids=committed_completion_ids,
            )
            if expiry_settled:
                if expiry_result is not None:
                    return expiry_result
                continue
            # User intent wins when both command kinds are waiting, matching the scheduled
            # lifecycle path: safely withdraw before a later organizer reschedule can write a new
            # calendar entry. This check also wins over a reminder due at the same timestamp.
            command_handled, result, terminal_result = await self._maybe_handle_handoff_command(
                inp,
                result,
                committed_completion_ids,
            )
            if terminal_result is not None:
                return terminal_result
            if command_handled:
                continue

            completion_handled, result, terminal_result = (
                await self._maybe_handle_ready_handoff_completion(
                    inp,
                    result,
                    completion_enabled=completion_enabled,
                    bounded_retry=bounded_completion_retry,
                    retry_not_before=completion_retry_not_before,
                    retry_counts=completion_retry_counts,
                    committed_completion_ids=committed_completion_ids,
                )
            )
            if terminal_result is not None:
                return terminal_result
            if completion_handled:
                continue

            reminder = self._next_handoff_reminder_due(
                created_at_value,
                deadline_at,
                sent_reminders,
                retry_not_before,
            )
            if reminder is not None and reminder[1] <= workflow.now():
                reminder_kind = reminder[0]
                reminder_result = await self._enqueue_handoff_reminder(
                    inp,
                    task_id,
                    expiry_transition_id,
                    reminder_kind,
                )
                if reminder_result.status in {"enqueued", "already_enqueued"}:
                    sent_reminders.add(reminder_kind)
                elif reminder_result.status == "not_due":
                    # Temporal and PostgreSQL clocks need not agree to the microsecond. Do not
                    # spin a workflow task while the database still owns the due decision.
                    retry_not_before[reminder_kind] = (
                        workflow.now() + _HANDOFF_REMINDER_DB_RETRY_DELAY
                    )
                elif reminder_result.status == "inactive":
                    # An external repair/terminal transition can resolve the task before its
                    # timer fires. It must never cause a new reminder identity or visible effect.
                    sent_reminders.update(HandoffReminderKind)
                else:
                    raise RuntimeError(
                        f"handoff reminder did not converge: {reminder_result.status}"
                    )
                continue

            await self._wait_for_handoff_task_event(
                deadline_at,
                reminder,
                completion_enabled=completion_enabled,
                bounded_completion_retry=bounded_completion_retry,
                completion_retry_not_before=completion_retry_not_before,
                committed_completion_ids=committed_completion_ids,
            )

    @staticmethod
    def _reset_handoff_task_retry_state(
        sent_reminders: set[HandoffReminderKind],
        reminder_retry_not_before: dict[HandoffReminderKind, datetime],
        completion_retry_not_before: dict[str, datetime],
        completion_retry_counts: dict[str, int],
        committed_completion_ids: set[str],
    ) -> None:
        """Reset timer state when recovery creates a distinct task identity."""
        sent_reminders.clear()
        reminder_retry_not_before.clear()
        completion_retry_not_before.clear()
        completion_retry_counts.clear()
        committed_completion_ids.clear()

    async def _maybe_handle_handoff_command(
        self,
        inp: RegChildInput,
        current: RegChildResult,
        committed_completion_ids: set[str],
    ) -> tuple[bool, RegChildResult, RegChildResult | None]:
        """Drain commands unless DB-confirmed completion recovery must establish workflow state."""
        if self._committed_handoff_completion_recovery_pending(
            committed_completion_ids
        ):
            return False, current, None
        if self._pending_unrsvp:
            current, terminal = await self._handle_handoff_unrsvp_signal(inp, current)
            return True, current, terminal
        if self._pending_organizer_changes:
            terminal = await self._handle_handoff_organizer_signal(inp)
            return True, current, terminal
        return False, current, None

    async def _maybe_settle_handoff_expiry(
        self,
        inp: RegChildInput,
        current: RegChildResult,
        task_id: str,
        deadline_at: datetime,
        *,
        completion_enabled: bool,
        bounded_retry: bool,
        completion_retry_not_before: dict[str, datetime],
        committed_completion_ids: set[str],
    ) -> tuple[bool, RegChildResult | None]:
        """Expire a due open task or recover a DB-confirmed completion instead of overwriting it."""
        recovery_pending = self._committed_handoff_completion_recovery_pending(
            committed_completion_ids
        )
        commands_can_precede_expiry = (
            not recovery_pending
            and (self._pending_unrsvp or self._pending_organizer_changes)
        )
        if (
            deadline_at > workflow.now()
            or commands_can_precede_expiry
            or self._handoff_completion_prevents_expiry(
                completion_enabled,
                bounded_retry,
                completion_retry_not_before,
                committed_completion_ids,
            )
        ):
            return False, None
        expiry_result = await self._expire_handoff_task(inp, current)
        if expiry_result.status != "completion_committed":
            return True, expiry_result
        self._mark_committed_handoff_completion_for_recovery(
            task_id,
            completion_retry_not_before,
            committed_completion_ids,
        )
        return True, None

    def _handoff_completion_prevents_expiry(
        self,
        completion_enabled: bool,
        bounded_retry: bool,
        retry_not_before: dict[str, datetime],
        committed_completion_ids: set[str],
    ) -> bool:
        """Let a new command receive one probe at the deadline, but never extend TTL on failure."""
        if not completion_enabled or not self._pending_handoff_completions:
            return False
        if not bounded_retry:
            return True
        return any(
            completion_id not in retry_not_before
            or completion_id in committed_completion_ids
            for completion_id in self._pending_handoff_completions
        )

    def _committed_handoff_completion_recovery_pending(
        self,
        committed_completion_ids: set[str],
    ) -> bool:
        """Whether the DB has confirmed success whose receipt replay must outrank commands."""
        return any(
            completion_id in committed_completion_ids
            for completion_id in self._pending_handoff_completions
        )

    def _mark_committed_handoff_completion_for_recovery(
        self,
        task_id: str,
        retry_not_before: dict[str, datetime],
        committed_completion_ids: set[str],
    ) -> None:
        """Force the exact receipt replay after expiry observes a completed durable task."""
        for completion_id, signal in self._pending_handoff_completions.items():
            if signal.task_id == task_id:
                committed_completion_ids.add(completion_id)
                retry_not_before.pop(completion_id, None)

    async def _maybe_handle_ready_handoff_completion(
        self,
        inp: RegChildInput,
        current: RegChildResult,
        *,
        completion_enabled: bool,
        bounded_retry: bool,
        retry_not_before: dict[str, datetime],
        retry_counts: dict[str, int],
        committed_completion_ids: set[str],
    ) -> tuple[bool, RegChildResult, RegChildResult | None]:
        """Run one due completion probe and project any retry into workflow-owned state."""
        if not completion_enabled or not self._pending_handoff_completions:
            return False, current, None
        completion_id = self._ready_handoff_completion_id(
            retry_not_before,
            workflow.now(),
        )
        if bounded_retry and completion_id is None:
            return False, current, None
        if completion_id is None:
            completion_id = next(iter(self._pending_handoff_completions))
        current, terminal_result, retry_after_seconds = (
            await self._handle_handoff_completion_signal(
                inp,
                current,
                completion_id,
                bounded_retry=bounded_retry,
            )
        )
        if retry_after_seconds is None:
            retry_not_before.pop(completion_id, None)
            retry_counts.pop(completion_id, None)
            committed_completion_ids.discard(completion_id)
        else:
            retry_count = retry_counts.get(completion_id, 0) + 1
            retry_counts[completion_id] = retry_count
            retry_not_before[completion_id] = workflow.now() + timedelta(
                seconds=self._handoff_completion_retry_seconds(
                    retry_count,
                    retry_after_seconds,
                )
            )
        return True, current, terminal_result

    @staticmethod
    def _handoff_completion_retry_seconds(
        retry_count: int,
        requested_seconds: float,
    ) -> float:
        """Apply a one-minute exponential floor capped at one hour to bound history growth."""
        if retry_count < 1:
            raise ValueError("handoff completion retry count must be positive")
        exponent = min(retry_count - 1, 6)
        workflow_floor: timedelta = min(
            _HANDOFF_COMPLETION_RETRY_DELAY * (2**exponent),
            _HANDOFF_COMPLETION_RETRY_CAP,
        )
        return max(requested_seconds, float(workflow_floor.total_seconds()))

    async def _wait_for_handoff_task_event(
        self,
        deadline_at: datetime,
        reminder: tuple[HandoffReminderKind, datetime] | None,
        *,
        completion_enabled: bool,
        bounded_completion_retry: bool,
        completion_retry_not_before: dict[str, datetime],
        committed_completion_ids: set[str],
    ) -> None:
        """Await a command, reminder, retry, or TTL without allowing retained evidence to spin."""
        completion_retry_at = self._next_handoff_completion_retry_at(
            completion_retry_not_before
        )
        committed_recovery_pending = any(
            completion_id in committed_completion_ids
            for completion_id in self._pending_handoff_completions
        )
        if committed_recovery_pending and deadline_at <= workflow.now():
            timer_at = completion_retry_at or (
                workflow.now() + _HANDOFF_COMPLETION_RETRY_CAP
            )
        else:
            timer_at = deadline_at if reminder is None else min(deadline_at, reminder[1])
            if bounded_completion_retry and completion_retry_at is not None:
                timer_at = min(timer_at, completion_retry_at)
        try:
            await workflow.wait_condition(
                lambda: (
                    not self._committed_handoff_completion_recovery_pending(
                        committed_completion_ids
                    )
                    and (
                        bool(self._pending_unrsvp)
                        or bool(self._pending_organizer_changes)
                    )
                )
                or (
                    completion_enabled
                    and (
                        (
                            not bounded_completion_retry
                            and bool(self._pending_handoff_completions)
                        )
                        or self._ready_handoff_completion_id(
                            completion_retry_not_before,
                            workflow.now(),
                        )
                        is not None
                    )
                ),
                timeout=max(timer_at - workflow.now(), timedelta(0)),
                timeout_summary="handoff-task-signal-reminder-or-expiry",
            )
        except TimeoutError:
            # The caller re-checks state so buffered commands win over a co-timed timer.
            return

    def _ready_handoff_completion_id(
        self,
        retry_not_before: dict[str, datetime],
        now: datetime,
    ) -> str | None:
        """Return the first exact completion whose workflow-owned retry timer is due."""
        for completion_id in self._pending_handoff_completions:
            retry_at = retry_not_before.get(completion_id)
            if retry_at is None or retry_at <= now:
                return completion_id
        return None

    def _next_handoff_completion_retry_at(
        self,
        retry_not_before: dict[str, datetime],
    ) -> datetime | None:
        """Return the earliest retry that still belongs to a retained completion command."""
        pending_retries = (
            retry_at
            for completion_id, retry_at in retry_not_before.items()
            if completion_id in self._pending_handoff_completions
        )
        return min(pending_retries, default=None)

    @staticmethod
    def _next_handoff_reminder_due(
        created_at: datetime,
        expires_at: datetime,
        sent_reminders: set[HandoffReminderKind],
        retry_not_before: dict[HandoffReminderKind, datetime],
    ) -> tuple[HandoffReminderKind, datetime] | None:
        """Return the earliest still-valid persisted task cadence, if any (FR-6.6, ADR-007)."""
        candidates: list[tuple[HandoffReminderKind, datetime]] = []
        for reminder_kind, cadence in (
            (HandoffReminderKind.T24H, timedelta(hours=24)),
            (HandoffReminderKind.T5D, timedelta(days=5)),
        ):
            if reminder_kind in sent_reminders:
                continue
            due_at = created_at + cadence
            # A reminder must never race a terminal task expiry. The persistence guard repeats
            # this check under lock; keeping it here avoids needless timer/activity history.
            if due_at >= expires_at:
                continue
            retry_at = retry_not_before.get(reminder_kind)
            if retry_at is not None and retry_at > due_at:
                due_at = retry_at
            candidates.append((reminder_kind, due_at))
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: (candidate[1], candidate[0].value))

    async def _enqueue_handoff_reminder(
        self,
        inp: RegChildInput,
        task_id: str,
        expiry_transition_id: str,
        reminder_kind: HandoffReminderKind,
    ) -> HandoffReminderActivityResult:
        """Execute one retry-safe cadence activity with its workflow-minted identity (FR-8.3)."""
        if self._keys is None:
            raise RuntimeError("retained handoff requires workflow-minted saga keys")
        reminder_id = (
            f"{self._keys.workflow_id}:{self._keys.run_id}:handoff-reminder:"
            f"{task_id}:{reminder_kind.value}:1"
        )
        result: HandoffReminderActivityResult = await workflow.execute_activity(
            "enqueue_handoff_reminder",
            HandoffReminderInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=self._keys.workflow_id,
                task_id=task_id,
                expiry_transition_id=expiry_transition_id,
                reminder_kind=reminder_kind.value,
                reminder_id=reminder_id,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=HandoffReminderActivityResult,
        )
        return result

    async def _handle_handoff_unrsvp_signal(
        self, inp: RegChildInput, current: RegChildResult
    ) -> tuple[RegChildResult, RegChildResult | None]:
        """Apply one buffered withdrawal before a reminder or organizer command (FR-8.8)."""
        if self._keys is None:
            raise RuntimeError("retained handoff requires workflow-minted saga keys")
        request_id = next(iter(self._pending_unrsvp))
        result = await self._unrsvp_with_recovery(inp, self._keys, request_id)
        if result.status == "pacing_wait":
            await self._pacer_backoff(result.retry_after_seconds)
            return current, None
        self._pending_unrsvp.pop(request_id, None)
        if result.status == "cancelled":
            return current, RegChildResult(status="cancelled", detail=result.detail)
        if result.status != "handoff":
            return current, None
        return (
            RegChildResult(
                status="handoff",
                handoff_task_id=result.handoff_task_id,
                handoff_expires_at=result.handoff_expires_at,
                handoff_expiry_transition_id=result.handoff_expiry_transition_id,
                handoff_created_at=result.handoff_created_at,
                detail=result.detail,
            ),
            None,
        )

    async def _handle_handoff_organizer_signal(self, inp: RegChildInput) -> RegChildResult | None:
        """Apply one buffered organizer change before reminder timer processing (FR-8.7/8.7a)."""
        if self._keys is None:
            raise RuntimeError("retained handoff requires workflow-minted saga keys")
        fingerprint = next(iter(self._pending_organizer_changes))
        organizer_signal = self._pending_organizer_changes[fingerprint]
        result = await self._reconcile_with_recovery(inp, self._keys, organizer_signal)
        self._pending_organizer_changes.pop(fingerprint, None)
        if result.status == "cancelled":
            return RegChildResult(status="cancelled", detail=result.detail)
        if result.status != "reconciled":
            return None
        if result.completion_deadline_at is None:
            raise RuntimeError("reconciled organizer change did not return a completion deadline")
        self._completion_deadline_at = result.completion_deadline_at
        lifecycle_result = await self._wait_for_lifecycle_work(inp, self._keys)
        if lifecycle_result is None:
            raise RuntimeError("reconciled lifecycle ended without a terminal child result")
        return lifecycle_result

    async def _handle_handoff_completion_signal(
        self,
        inp: RegChildInput,
        current: RegChildResult,
        completion_id: str,
        *,
        bounded_retry: bool,
    ) -> tuple[RegChildResult, RegChildResult | None, float | None]:
        """Verify one mark-done out of band, then schedule or retain a fail-closed review.

        The signal carries no claim that registration succeeded. The existing confirmation
        activity name performs a fresh provider read and freeBusy gate, atomically advancing to
        REGISTERED only after both settle safely. Its stable completion id makes API/Temporal
        redelivery converge. New histories make each dependency probe one activity attempt, then
        retain this exact signal behind an interruptible workflow timer (FR-6.3, FR-8.3, FR-16).
        """
        if self._keys is None:
            raise RuntimeError("retained handoff requires workflow-minted saga keys")
        signal = self._pending_handoff_completions[completion_id]
        if signal.task_id != current.handoff_task_id:
            self._pending_handoff_completions.pop(completion_id, None)
            self._handled_handoff_completion_ids.add(completion_id)
            return current, None, None

        if bounded_retry:
            # The database/API/watch guards remain the authority for factual commands. Opening
            # these workflow buffers before verification closes the commit-to-activity-ACK gap:
            # if verified completion commits REGISTERED and the ACK is lost, organizer and
            # authorized un-RSVP signals are journaled until the exact receipt replay succeeds.
            self._open_organizer_change_buffer(inp)
            self._activate_post_booking_lifecycle(inp)

        verification_input = AwaitConfirmationInput(
            tenant_id=inp.tenant_id,
            canonical_event_id=inp.canonical_event_id,
            workflow_id=self._keys.workflow_id,
            lane=Lane.HANDOFF.value,
            source_outcome=RegisterOutcome.FAILED.value,
            awaiting_transition_id=self._keys.awaiting_transition_id,
            registered_transition_id=self._keys.registered_transition_id,
            confirmation_read_queue_item_id=(
                f"{self._keys.workflow_id}:{self._keys.run_id}:"
                f"handoff-verification:{signal.task_id}:1"
            ),
            handoff_task_id=signal.task_id,
            handoff_completion_id=signal.completion_id,
        )
        try:
            if bounded_retry:
                verification = await workflow.execute_activity(
                    "await_confirmation",
                    verification_input,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    result_type=AwaitConfirmationResult,
                )
            else:
                # Replay compatibility for histories that predate bounded dependency probes.
                verification = await workflow.execute_activity(
                    "await_confirmation",
                    verification_input,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    result_type=AwaitConfirmationResult,
                )
        except ActivityError:
            if not bounded_retry:
                raise
            return (
                current,
                None,
                _HANDOFF_COMPLETION_RETRY_DELAY.total_seconds(),
            )
        if verification.pacing_status is not None:
            # WAIT, projected DEGRADE, and SATURATED are all non-authoritative admission outcomes.
            # Retain the exact completion signal and retry after the workflow timer; none may burn
            # the user's one-time evidence or silently close the completion buffer.
            if bounded_retry:
                return (
                    current,
                    None,
                    max(verification.retry_after_seconds or 0.0, 0.001),
                )
            await self._pacer_backoff(verification.retry_after_seconds)
            return current, None, None

        self._pending_handoff_completions.pop(completion_id, None)
        self._handled_handoff_completion_ids.add(completion_id)
        self._handoff_completion_buffer_open = False
        if verification.status != "confirmed":
            return current, None, None

        self._open_organizer_change_buffer(inp)
        self._activate_post_booking_lifecycle(inp)
        calendar_result, scheduled = await self._calendar_stage(
            inp,
            self._keys,
            Lane.HANDOFF.value,
        )
        if not scheduled:
            return calendar_result, None, None
        lifecycle_result = await self._wait_for_lifecycle_work(inp, self._keys)
        if lifecycle_result is None:
            raise RuntimeError("scheduled handoff completion ended without a terminal lifecycle")
        return current, lifecycle_result, None

    async def _expire_handoff_task(
        self, inp: RegChildInput, result: RegChildResult
    ) -> RegChildResult:
        """Await one durable task deadline then invoke its once-minted guarded expiry activity."""
        task_id = result.handoff_task_id
        expiry = result.handoff_expires_at
        expiry_transition_id = result.handoff_expiry_transition_id
        if task_id is None or expiry is None or expiry_transition_id is None:
            raise RuntimeError("a retained handoff requires persisted task expiry context")
        deadline_at = self._parse_completion_deadline(expiry)
        if deadline_at > workflow.now():
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda: False,
                    timeout=deadline_at - workflow.now(),
                    timeout_summary="handoff-task-expiry",
                )
        expired: ExpireHandoffResult = await workflow.execute_activity(
            "expire_handoff",
            ExpireHandoffInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=(self._keys.workflow_id if self._keys is not None else ""),
                task_id=task_id,
                expiry_transition_id=expiry_transition_id,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=ExpireHandoffResult,
        )
        if expired.status == "expired":
            return RegChildResult(
                status="expired",
                handoff_task_id=task_id,
                detail=expired.detail,
            )
        if expired.status == "completion_committed":
            return RegChildResult(
                status="completion_committed",
                handoff_task_id=task_id,
                handoff_expires_at=expiry,
                handoff_expiry_transition_id=expiry_transition_id,
                handoff_created_at=result.handoff_created_at,
                detail=expired.detail,
            )
        if expired.terminal_state is not None:
            return RegChildResult(
                status=expired.terminal_state,
                handoff_task_id=task_id,
                detail=expired.detail,
            )
        raise RuntimeError(f"handoff expiry could not terminalize lifecycle: {expired.detail}")

    async def _wait_for_lifecycle_work(
        self, inp: RegChildInput, keys: RegistrationSagaKeys
    ) -> RegChildResult | None:
        """Drain post-booking commands or complete quietly at the activity-derived deadline.

        The workflow keeps an unprocessed command in its durable queue across an activity failure.
        A retried un-RSVP activity always begins with the remote read and reuses the same mutation
        key, so a lost provider acknowledgement converges rather than repeats a source effect. A
        ``wait_condition`` timeout is a durable Temporal timer; a reschedule result replaces its
        absolute deadline rather than consulting worker wall-clock time (ADR-003/005/007/008).
        """
        while True:
            deadline = self._completion_deadline_at
            if deadline is None:
                await workflow.wait_condition(
                    lambda: bool(self._pending_unrsvp) or bool(self._pending_organizer_changes),
                    timeout_summary="post-booking-lifecycle",
                )
            else:
                deadline_at = self._parse_completion_deadline(deadline)
                if (
                    deadline_at <= workflow.now()
                    and not self._pending_unrsvp
                    and not self._pending_organizer_changes
                ):
                    completion = await self._complete_quiet_lifecycle(inp, keys)
                    if completion is not None:
                        return completion
                    continue

                def lifecycle_work_arrived(expected_deadline: str = deadline) -> bool:
                    """Wake the current timer only for a queued command or a replacement deadline."""
                    return (
                        bool(self._pending_unrsvp)
                        or bool(self._pending_organizer_changes)
                        or self._completion_deadline_at != expected_deadline
                    )

                try:
                    await workflow.wait_condition(
                        lifecycle_work_arrived,
                        timeout=max(deadline_at - workflow.now(), timedelta(0)),
                        timeout_summary="post-booking-lifecycle",
                    )
                except TimeoutError:
                    if (
                        self._completion_deadline_at == deadline
                        and not self._pending_unrsvp
                        and not self._pending_organizer_changes
                    ):
                        completion = await self._complete_quiet_lifecycle(inp, keys)
                        if completion is not None:
                            return completion
                    continue

            if self._completion_deadline_at != deadline:
                # A successful reschedule result replaced the timer while this workflow was idle.
                continue
            # User intent wins when a reschedule and un-RSVP arrive together: remove their
            # calendar entry and attempt withdrawal before processing a later organizer update.
            if self._pending_unrsvp:
                request_id = next(iter(self._pending_unrsvp))
                result = await self._unrsvp_with_recovery(inp, keys, request_id)
                if result.status == "pacing_wait":
                    await self._pacer_backoff(result.retry_after_seconds)
                    continue
                self._pending_unrsvp.pop(request_id, None)
                if result.status == "cancelled":
                    return RegChildResult(status="cancelled", detail=result.detail)
                if result.status == "handoff":
                    return await self._wait_for_handoff_task(
                        inp,
                        RegChildResult(
                            status="handoff",
                            handoff_task_id=result.handoff_task_id,
                            handoff_expires_at=result.handoff_expires_at,
                            handoff_expiry_transition_id=result.handoff_expiry_transition_id,
                            handoff_created_at=result.handoff_created_at,
                            detail=result.detail,
                        ),
                    )
                continue

            fingerprint = next(iter(self._pending_organizer_changes))
            signal = self._pending_organizer_changes[fingerprint]
            reconcile_result = await self._reconcile_with_recovery(inp, keys, signal)
            self._pending_organizer_changes.pop(fingerprint, None)
            if reconcile_result.status == "cancelled":
                return RegChildResult(status="cancelled", detail=reconcile_result.detail)
            if reconcile_result.status == "reconciled":
                if reconcile_result.completion_deadline_at is None:
                    raise RuntimeError(
                        "reconciled organizer change did not return a completion deadline"
                    )
                self._completion_deadline_at = reconcile_result.completion_deadline_at

    async def _complete_quiet_lifecycle(
        self, inp: RegChildInput, keys: RegistrationSagaKeys
    ) -> RegChildResult | None:
        """Run the once-keyed terminal transition only after the durable quiet-period timer fires."""
        result: CompleteLifecycleResult = await workflow.execute_activity(
            "complete_lifecycle",
            CompleteLifecycleInput(
                tenant_id=inp.tenant_id,
                canonical_event_id=inp.canonical_event_id,
                workflow_id=keys.workflow_id,
                completed_transition_id=keys.completed_transition_id,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=CompleteLifecycleResult,
        )
        if result.status == "completed":
            return RegChildResult(status="completed", detail=result.detail)
        # A non-quiet lifecycle (for example a manual withdrawal handoff) must remain owned by
        # this child, but the elapsed terminalization deadline must not create a busy loop.
        self._completion_deadline_at = None
        return None

    @staticmethod
    def _parse_completion_deadline(value: str) -> datetime:
        """Decode the activity-recorded, timezone-aware deadline without reading a worker clock."""
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("lifecycle completion deadline must be timezone-aware")
        return deadline

    async def _reconcile_with_recovery(
        self,
        inp: RegChildInput,
        keys: RegistrationSagaKeys,
        signal: OrganizerChangeSignal,
    ) -> ReconcileOrganizerChangeResult:
        """Retry calendar/ledger reconciliation with its one workflow-minted transition identity."""
        command = ReconcileOrganizerChangeInput(
            tenant_id=inp.tenant_id,
            canonical_event_id=inp.canonical_event_id,
            workflow_id=keys.workflow_id,
            fingerprint=signal.fingerprint,
            source=signal.source,
            event_status=signal.event_status,
            transition_id=f"{keys.workflow_id}:{keys.run_id}:reconcile:{signal.fingerprint}:1",
            start_at=signal.start_at,
            end_at=signal.end_at,
            time_zone=signal.time_zone,
            title=signal.title,
            venue_name=signal.venue_name,
        )
        while True:
            try:
                result = await workflow.execute_activity(
                    "reconcile_organizer_change",
                    command,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    result_type=ReconcileOrganizerChangeResult,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                await workflow.sleep(timedelta(seconds=1))
                continue
            reconciled = cast(ReconcileOrganizerChangeResult, result)
            # Only a completed activity is durable enough to suppress a serial fanout
            # redelivery. A failed activity remains unhandled and retries with its same
            # workflow-minted transition identity (ADR-003/ADR-008).
            if self._organizer_change_serial_dedup_enabled:
                self._handled_organizer_change_fingerprints.add(signal.fingerprint)
            return reconciled

    async def _unrsvp_with_recovery(
        self, inp: RegChildInput, keys: RegistrationSagaKeys, request_id: str
    ) -> UnrsvpActivityResult:
        """Retry a failed withdrawal only through the source's fresh state read (FR-8.8)."""
        command = UnrsvpInput(
            tenant_id=inp.tenant_id,
            canonical_event_id=inp.canonical_event_id,
            workflow_id=keys.workflow_id,
            request_id=request_id,
            withdrawing_transition_id=(
                f"{keys.workflow_id}:{keys.run_id}:unrsvp:{request_id}:withdrawing:1"
            ),
            cancelled_transition_id=(
                f"{keys.workflow_id}:{keys.run_id}:unrsvp:{request_id}:cancelled:1"
            ),
            handoff_expiry_transition_id=(
                f"{keys.workflow_id}:{keys.run_id}:unrsvp:{request_id}:expired:1"
            ),
            source_read_queue_item_id=(
                f"{keys.workflow_id}:{keys.run_id}:unrsvp:{request_id}:read:1"
            ),
            source_mutation_idempotency_key=(
                f"{keys.workflow_id}:{keys.run_id}:unrsvp:{request_id}:withdraw:1"
            ),
        )
        while True:
            try:
                result = await workflow.execute_activity(
                    "unrsvp",
                    command,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    result_type=UnrsvpActivityResult,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                # The activity can have crossed the source boundary before its ACK was lost.  Its
                # next execution reads state first using the exact same key rather than blind retry.
                await workflow.sleep(timedelta(seconds=1))
                continue
            return cast(UnrsvpActivityResult, result)

    async def _report_to_parent(self, outcome: ChildOutcome) -> None:
        """Signal the currently-running parent with a deduplicable outcome record (ADR-003)."""
        parent = workflow.info().parent
        if parent is None:
            return
        handle = workflow.get_external_workflow_handle(parent.workflow_id, run_id=parent.run_id)
        await handle.signal("child_outcome", outcome)


@workflow.defn
class EventRequestWorkflow:
    """Short-lived parent that owns discovery/ranking and the FR-5.0 attempt loop."""

    def __init__(self) -> None:
        self._child_outcomes: dict[str, ChildOutcome] = {}

    @workflow.signal
    def child_outcome(self, outcome: ChildOutcome) -> None:
        """Receive one child outcome; repeated signals overwrite the same event key safely."""
        self._child_outcomes[outcome.canonical_event_id] = outcome

    @workflow.run
    async def run(self, inp: RequestInput) -> RequestResult:
        disc: DiscoverResult = await workflow.execute_activity(
            "discover_and_rank",
            inp,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=DiscoverResult,
        )
        attempts = 0
        candidate_ids = disc.candidate_ids[: inp.attempt_budget]
        failed_children: dict[
            str,
            tuple[
                workflow.ChildWorkflowHandle[RegistrationWorkflow, RegChildResult],
                ChildOutcome,
            ],
        ] = {}
        uses_directive_terminality = False
        for canonical_id in candidate_ids:
            attempts += 1
            child = await workflow.start_child_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=inp.tenant_id,
                    canonical_event_id=canonical_id,
                    keep_open_after_scheduling=True,
                ),
                id=f"{inp.tenant_id}:{canonical_id}",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
            outcome = await self._wait_for_child_outcome(canonical_id)
            if outcome.status == "failed":
                uses_directive_terminality = workflow.patched("p3c-parent-directive-terminality-v1")
                if not uses_directive_terminality:
                    # Preserve the historic command/history sequence for pre-P3c parent runs.
                    directive = (
                        _DIRECTIVE_DEMOTE_TO_HANDOFF
                        if attempts == len(candidate_ids)
                        else _DIRECTIVE_CLOSE
                    )
                    await child.signal("directive", directive)
                    if directive == _DIRECTIVE_DEMOTE_TO_HANDOFF:
                        terminal_outcome = await self._wait_for_child_outcome(canonical_id)
                        if terminal_outcome.status != "handoff":
                            raise RuntimeError(
                                "terminal handoff directive did not produce a handoff child outcome"
                            )
                        return RequestResult(
                            outcome=terminal_outcome.status,
                            attempts=attempts,
                            detail=f"lane={terminal_outcome.lane}",
                        )
                    result: RegChildResult = await child
                    continue
                # Keep every newly failed child parked while the bounded attempt loop proceeds.
                # Once all outcomes are known, ADR-003 requires the highest-ranked still-open
                # handoff-eligible child—not merely the final attempt—to receive the terminal
                # handoff directive. The ten-minute child timeout remains the safe fallback when
                # another candidate itself waits too long (FR-5.0, AC-33, ADR-003).
                failed_children[canonical_id] = (child, outcome)
                continue

            if outcome.status in {"registered", "handoff"}:
                # The child has reported either the committed calendar write or a durable manual
                # task and is now its own lifecycle/TTL owner. Awaiting it here would make the
                # request workflow long-lived and contradict the ADR-003 split (FR-6.6/8.1/8.7).
                if uses_directive_terminality:
                    await self._close_failed_children(failed_children)
                return RequestResult(
                    outcome=outcome.status, attempts=attempts, detail=f"lane={outcome.lane}"
                )
            result = await child
            if result.status == "handoff":
                if uses_directive_terminality:
                    await self._close_failed_children(failed_children)
                return RequestResult(
                    outcome=result.status, attempts=attempts, detail=f"lane={result.lane}"
                )
        if uses_directive_terminality:
            terminal_handoff = await self._demote_best_handoff_child(failed_children)
            if terminal_handoff is not None:
                return RequestResult(
                    outcome=terminal_handoff.status,
                    attempts=attempts,
                    detail=f"lane={terminal_handoff.lane}",
                )
            await self._close_failed_children(failed_children)
        return await self._finalize_no_candidate(inp, attempts)

    async def _demote_best_handoff_child(
        self,
        failed_children: dict[
            str,
            tuple[
                workflow.ChildWorkflowHandle[RegistrationWorkflow, RegChildResult],
                ChildOutcome,
            ],
        ],
    ) -> ChildOutcome | None:
        """Choose the highest-ranked viable parked child for one terminal handoff (ADR-003).

        Insertion order is discovery/ranking order because the parent records children exactly as
        their bounded attempt signals arrive.  A child that timed out before the parent returns to
        it has already sent a terminal close outcome; skip it and choose the next eligible child.
        """
        for canonical_event_id, (child, initial) in tuple(failed_children.items()):
            if not initial.handoff_eligible:
                continue
            terminal = await self._direct_failed_child(
                canonical_event_id, child, _DIRECTIVE_DEMOTE_TO_HANDOFF
            )
            failed_children.pop(canonical_event_id, None)
            if terminal.status == "handoff":
                await self._close_failed_children(failed_children)
                return terminal
            self._require_candidate_terminal(terminal, "handoff directive")
        return None

    async def _close_failed_children(
        self,
        failed_children: dict[
            str,
            tuple[
                workflow.ChildWorkflowHandle[RegistrationWorkflow, RegChildResult],
                ChildOutcome,
            ],
        ],
    ) -> None:
        """Release all parked children before a request returns on another outcome (ADR-007)."""
        for canonical_event_id, (child, _) in tuple(failed_children.items()):
            terminal = await self._direct_failed_child(canonical_event_id, child, _DIRECTIVE_CLOSE)
            failed_children.pop(canonical_event_id, None)
            self._require_candidate_terminal(terminal, "close directive")

    async def _direct_failed_child(
        self,
        canonical_event_id: str,
        child: workflow.ChildWorkflowHandle[RegistrationWorkflow, RegChildResult],
        directive: str,
    ) -> ChildOutcome:
        """Send a directive unless the child already journaled its timeout cleanup outcome."""
        if canonical_event_id not in self._child_outcomes:
            await child.signal("directive", directive)
        return await self._wait_for_child_outcome(canonical_event_id)

    @staticmethod
    def _require_candidate_terminal(outcome: ChildOutcome, directive: str) -> None:
        """Reject a malformed child protocol reply before the parent declares its request terminal."""
        if outcome.status not in {
            LifecycleState.FAILED_NO_CANDIDATE.value,
            LifecycleState.CANCELLED.value,
        }:
            raise RuntimeError(f"candidate {directive} did not produce a terminal child outcome")

    async def _finalize_no_candidate(self, inp: RequestInput, attempts: int) -> RequestResult:
        """Commit the parent-only no-result outcome when no child handoff was retained.

        Empty discovery has no canonical event and therefore no lifecycle row to transition.  The
        request terminal activity supplies the same durable outbox/no-ACK convergence expected of
        lifecycle terminals, with a workflow-minted identity (FR-5.0/6.6, ADR-003/007).
        """
        if not workflow.patched("p3c-request-no-result-v1"):
            return RequestResult(
                outcome=LifecycleState.FAILED_NO_CANDIDATE.value, attempts=attempts
            )
        result: FinalizeNoCandidateResult = await workflow.execute_activity(
            "finalize_no_candidate",
            FinalizeNoCandidateInput(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                transition_id=f"{workflow.info().workflow_id}:failed-no-candidate:1",
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            result_type=FinalizeNoCandidateResult,
        )
        if result.status not in {"finalized", "already_finalized"}:
            raise RuntimeError("request no-result terminalization did not converge")
        return RequestResult(outcome=LifecycleState.FAILED_NO_CANDIDATE.value, attempts=attempts)

    async def _wait_for_child_outcome(self, canonical_event_id: str) -> ChildOutcome:
        """Suspend on this child only; the parameter avoids a loop-closure capture (ADR-003)."""
        await workflow.wait_condition(lambda: canonical_event_id in self._child_outcomes)
        return self._child_outcomes.pop(canonical_event_id)
