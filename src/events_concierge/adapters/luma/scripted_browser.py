"""Fixture-only BrowserRsvpPort implementation for Luma contract tests (FR-5.5, ADR-003/006).

This is intentionally not a browser automation client.  It makes no network calls and has no
credential surface; tests script sanitized typed observations by Luma source-event ID.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from ...ports.browser import BrowserRsvpObservation
from ...ports.sources import RegistrationTarget


class BrowserAcknowledgementLostError(RuntimeError):
    """Simulated crash after a browser submit has taken effect but before its acknowledgement."""


@dataclass(frozen=True, slots=True)
class ScriptedBrowserScenario:
    """A synthetic before/after submit state for one Luma event fixture."""

    initial_detection: BrowserRsvpObservation
    after_submit: BrowserRsvpObservation
    raise_after_effect_once: bool = False


class ScriptedBrowserRsvpDriver:
    """Deterministic BrowserRsvpPort for sanitized fixtures, never a live browser (FR-5.5).

    The driver records submit attempts and makes ``after_submit`` visible to later detects.  This
    models the lost-ACK crash seam required by ADR-003: replay detects the remote confirmation
    rather than issuing a second submit.
    """

    def __init__(self, scenarios: Mapping[str, ScriptedBrowserScenario]) -> None:
        self._scenarios = dict(scenarios)
        self._submitted: set[tuple[UUID, str]] = set()
        self._lost_acknowledgements: set[tuple[UUID, str]] = set()
        self.detect_calls = 0
        self.submit_calls = 0
        self.idempotency_keys: list[str] = []

    async def detect(self, tenant_id: UUID, target: RegistrationTarget) -> BrowserRsvpObservation:
        self.detect_calls += 1
        scenario = self._scenario(target)
        return (
            scenario.after_submit
            if self._key(tenant_id, target) in self._submitted
            else scenario.initial_detection
        )

    async def submit(
        self, tenant_id: UUID, target: RegistrationTarget, idempotency_key: str
    ) -> BrowserRsvpObservation:
        scenario = self._scenario(target)
        key = self._key(tenant_id, target)
        self.submit_calls += 1
        self.idempotency_keys.append(idempotency_key)
        self._submitted.add(key)
        if scenario.raise_after_effect_once and key not in self._lost_acknowledgements:
            self._lost_acknowledgements.add(key)
            raise BrowserAcknowledgementLostError(
                "fixture browser acknowledgement lost after submit"
            )
        return scenario.after_submit

    def _scenario(self, target: RegistrationTarget) -> ScriptedBrowserScenario:
        try:
            return self._scenarios[target.source_event_id]
        except KeyError as error:
            raise ValueError("no scripted Luma scenario for registration target") from error

    @staticmethod
    def _key(tenant_id: UUID, target: RegistrationTarget) -> tuple[UUID, str]:
        return tenant_id, target.source_event_id
