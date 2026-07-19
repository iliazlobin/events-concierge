"""Lane router -- a pure, versioned decision function over (source, modality, group-condition).

Never a model choice (FR-3.1/FR-5.1, ADR-003). ToS-prohibited modalities are REFUSED rows so they
are structurally unreachable. The lane plan orders lanes autonomous-on-SLA -> browser-best-effort ->
handoff for the FR-5.0 within-candidate attempt ladder."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import GroupCondition, Lane, Modality, Source
from .policy import SourcePolicy


@dataclass(frozen=True, slots=True)
class LaneInput:
    source: Source
    modality: Modality
    policy: SourcePolicy
    group_condition: GroupCondition = GroupCondition.UNKNOWN
    credential_healthy: bool = True
    meetup_autojoin_enabled: bool = False  # default OFF pending the G2 spike (ADR-005)


def route_single(inp: LaneInput) -> Lane:
    """Decide the lane for one (source, modality). Order of checks encodes the ADR-003 table."""
    src, mod = inp.source, inp.modality

    # Structurally-refused, ToS-prohibited modalities (FR-3.2).
    if src is Source.MEETUP and mod is Modality.BROWSER:
        return Lane.REFUSED
    if src is Source.LUMA and mod is Modality.API:
        return Lane.REFUSED

    if src is Source.PARTIFUL:
        return Lane.DISABLED

    if src is Source.MEETUP and mod is Modality.API:
        if not inp.policy.allows(Modality.API):
            return Lane.HANDOFF
        gc = inp.group_condition
        if gc is GroupCondition.MEMBER:
            return Lane.AUTONOMOUS_SLA
        if gc is GroupCondition.OPEN_INSTANT_JOIN and inp.meetup_autojoin_enabled:
            return Lane.AUTONOMOUS_SLA
        # non-member / approval / dues / unknown / open-without-flag -> human join gate.
        return Lane.HANDOFF

    if src is Source.LUMA and mod is Modality.BROWSER:
        if inp.policy.allows(Modality.BROWSER) and inp.credential_healthy:
            return Lane.BROWSER_BEST_EFFORT
        return Lane.HANDOFF

    # Eventbrite, Ticketmaster, SerpApi funnel, and the free-crawl source are discovery-only:
    # registration always routes to a pre-filled handoff (FR-5.6, FR-10.2).
    return Lane.HANDOFF


_LANE_ORDER = {Lane.AUTONOMOUS_SLA: 0, Lane.BROWSER_BEST_EFFORT: 1, Lane.HANDOFF: 2}


def build_lane_plan(inputs: list[LaneInput]) -> tuple[Lane, ...]:
    """Order the registerable lanes for a CanonicalEvent across its (source, modality) options,
    dropping REFUSED/DISABLED and de-duplicating. Empty registerable set -> a handoff-only plan."""
    lanes = {route_single(i) for i in inputs}
    plan = sorted(
        (lane for lane in lanes if lane in _LANE_ORDER),
        key=lambda lane: _LANE_ORDER[lane],
    )
    return tuple(plan) if plan else (Lane.HANDOFF,)
