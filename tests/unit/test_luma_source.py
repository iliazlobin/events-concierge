"""Fixture tests for Luma detect-then-submit safety (FR-5.4/5.5/5.10, ADR-003/006)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.luma.scripted_browser import (
    BrowserAcknowledgementLostError,
    ScriptedBrowserRsvpDriver,
    ScriptedBrowserScenario,
)
from events_concierge.adapters.luma.source import LumaSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.domain.enums import Modality, RsvpState, Source
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.browser import BrowserRsvpObservation, BrowserRsvpStatus
from events_concierge.ports.sources import RegisterOutcome, RegistrationTarget

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/luma/browser_rsvp_observations.json"


def _observation(name: str) -> BrowserRsvpObservation:
    fixtures = cast(dict[str, dict[str, object]], json.loads(_FIXTURE_PATH.read_text()))
    fixture = fixtures[name]
    status = fixture["status"]
    price_cents = fixture["price_cents"]
    assert isinstance(status, str)
    assert price_cents is None or (
        isinstance(price_cents, int) and not isinstance(price_cents, bool)
    )
    return BrowserRsvpObservation(BrowserRsvpStatus(status), price_cents)


def _target(source_event_id: str = "luma-fixture-event") -> RegistrationTarget:
    return RegistrationTarget(
        source_event_id=source_event_id,
        registration_url=f"https://fixtures.invalid/luma/{source_event_id}",
    )


def _source(
    initial: BrowserRsvpObservation,
    after_submit: BrowserRsvpObservation,
    *,
    raise_after_effect_once: bool = False,
) -> tuple[LumaSource, ScriptedBrowserRsvpDriver]:
    driver = ScriptedBrowserRsvpDriver(
        {
            "luma-fixture-event": ScriptedBrowserScenario(
                initial_detection=initial,
                after_submit=after_submit,
                raise_after_effect_once=raise_after_effect_once,
            )
        }
    )
    return LumaSource(driver, policy=_allowing_policy()), driver


def _allowing_policy() -> DataPolicyEngine:
    """Build the source-local ADR-004 guard used by the offline browser adapter."""
    return DataPolicyEngine(
        source_policies={
            Source.LUMA: SourcePolicy(
                source=Source.LUMA,
                automation_allowed={Modality.BROWSER: True},
            )
        }
    )


class _KillSwitchAfterDetectDriver(ScriptedBrowserRsvpDriver):
    """Engage policy after Luma's detect but before its possible browser submit."""

    def __init__(
        self,
        scenarios: dict[str, ScriptedBrowserScenario],
        policy: DataPolicyEngine,
    ) -> None:
        super().__init__(scenarios)
        self._policy = policy

    async def detect(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> BrowserRsvpObservation:
        observation = await super().detect(tenant_id, target)
        self._policy.set_kill_switch(True)
        return observation


@pytest.mark.asyncio
async def test_luma_lost_ack_redetects_confirmed_without_a_second_submit() -> None:
    source, driver = _source(
        _observation("not_present_free"),
        _observation("confirmed"),
        raise_after_effect_once=True,
    )
    tenant_id = uuid4()
    target = _target()
    key = "workflow:source:1"

    with pytest.raises(BrowserAcknowledgementLostError):
        await source.register(tenant_id, target, Modality.BROWSER, key)

    assert driver.submit_calls == 1
    assert (
        await source.read_registration_state(tenant_id, target, Modality.BROWSER)
        is RsvpState.CONFIRMED
    )
    retried = await source.register(tenant_id, target, Modality.BROWSER, key)

    assert retried.outcome is RegisterOutcome.NO_OP_ALREADY_CONFIRMED
    assert driver.submit_calls == 1
    assert driver.idempotency_keys == [key]


@pytest.mark.asyncio
async def test_luma_submits_only_after_a_fresh_free_not_present_detection() -> None:
    source, driver = _source(_observation("not_present_free"), _observation("confirmed"))
    tenant_id = uuid4()
    target = _target()

    initial_state = await source.read_registration_state(tenant_id, target, Modality.BROWSER)
    result = await source.register(tenant_id, target, Modality.BROWSER, "workflow:source:1")

    assert initial_state is RsvpState.NOT_PRESENT
    assert result.outcome is RegisterOutcome.CONFIRMED
    assert driver.detect_calls == 2
    assert driver.submit_calls == 1


@pytest.mark.asyncio
async def test_luma_fails_closed_without_its_source_local_pre_submit_policy_guard() -> None:
    """A direct fixture construction cannot bypass composition's data-plane guard."""
    driver = ScriptedBrowserRsvpDriver(
        {
            "luma-fixture-event": ScriptedBrowserScenario(
                initial_detection=_observation("not_present_free"),
                after_submit=_observation("confirmed"),
            )
        }
    )
    source = LumaSource(driver)

    result = await source.register(uuid4(), _target(), Modality.BROWSER, "workflow:source:1")

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert driver.detect_calls == 1
    assert driver.submit_calls == 0


@pytest.mark.asyncio
async def test_luma_rechecks_policy_after_detect_before_its_browser_submit() -> None:
    """A kill switch engaging during detect prevents a later Luma browser click (ADR-004)."""
    policy = _allowing_policy()
    driver = _KillSwitchAfterDetectDriver(
        {
            "luma-fixture-event": ScriptedBrowserScenario(
                initial_detection=_observation("not_present_free"),
                after_submit=_observation("confirmed"),
            )
        },
        policy,
    )
    source = LumaSource(driver, policy=policy)

    result = await source.register(uuid4(), _target(), Modality.BROWSER, "workflow:source:1")

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert "kill switch" in result.detail
    assert driver.detect_calls == 1
    assert driver.submit_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected_outcome", "expected_state"),
    [
        ("ambiguous", RegisterOutcome.NEEDS_HANDOFF, RsvpState.AMBIGUOUS),
        ("login_required", RegisterOutcome.NEEDS_REAUTH, RsvpState.AMBIGUOUS),
        ("paywall", RegisterOutcome.PAYWALL, RsvpState.AMBIGUOUS),
        ("not_present_unknown_price", RegisterOutcome.NEEDS_HANDOFF, RsvpState.NOT_PRESENT),
    ],
)
async def test_luma_unsafe_detection_never_submits(
    fixture_name: str, expected_outcome: RegisterOutcome, expected_state: RsvpState
) -> None:
    source, driver = _source(_observation(fixture_name), _observation("confirmed"))
    tenant_id: UUID = uuid4()
    target = _target()

    result = await source.register(tenant_id, target, Modality.BROWSER, "workflow:source:1")

    assert (
        await source.read_registration_state(tenant_id, target, Modality.BROWSER) is expected_state
    )
    assert result.outcome is expected_outcome
    assert driver.submit_calls == 0
