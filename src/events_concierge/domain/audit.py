"""PII-minimized immutable registration-action audit vocabulary (FR-7.3, NFR-8/10).

Lifecycle transitions already have their own guarded transition ledger. These records cover the
separate policy and source-RSVP observations that must remain traceable across Temporal activity
retries. They deliberately carry opaque identities and closed outcomes only: no request text, event
title, URL, credential, OTP, provider payload, or free-form error detail belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from .enums import Modality, Source


class RegistrationActionAuditPhase(StrEnum):
    """The three P6a registration observations, each keyed from one minted source key."""

    POLICY_PRECHECK = "policy_precheck"
    POLICY_PRE_MUTATE = "policy_pre_mutate"
    SOURCE_RSVP_OUTCOME = "source_rsvp_outcome"


class RegistrationActionAuditDecision(StrEnum):
    """The closed authorization result attached to a policy/RVSP observation."""

    ALLOWED = "allowed"
    DENIED = "denied"


class RegistrationActionAuditOutcome(StrEnum):
    """The normalized, settled source-RSVP results that are safe to retain."""

    CONFIRMED = "confirmed"
    PENDING_CONFIRMATION = "pending_confirmation"
    NEEDS_HANDOFF = "needs_handoff"
    PAYWALL = "paywall"
    NEEDS_REAUTH = "needs_reauth"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RegistrationActionAudit:
    """One retry-stable, PII-free registration fact (FR-7.3, NFR-8/10, ADR-003/007).

    The audit key derives only from the child workflow's once-minted source key and a fixed phase
    suffix. This converges an ACK-loss replay on the same immutable evidence. P14c registration
    paths bind an opaque owner-seeded consent ref to every newly allowed fact; a denied early
    precheck can record its explicit absence. Historical P6a rows with ``consent_ref=None`` remain
    representable solely so their exact ACK-loss replay can converge (FR-2.9, FR-7.3, NFR-8/10).
    """

    audit_key: str
    tenant_id: UUID
    workflow_id: str
    source: Source
    modality: Modality
    phase: RegistrationActionAuditPhase
    policy_decision: RegistrationActionAuditDecision
    outcome: RegistrationActionAuditOutcome | None = None
    consent_ref: UUID | None = None

    def __post_init__(self) -> None:
        if not self.audit_key or not self.workflow_id:
            raise ValueError("registration action-audit identities must be non-empty")
        _validate_workflow_id(self.tenant_id, self.workflow_id)
        if self.phase in (
            RegistrationActionAuditPhase.POLICY_PRECHECK,
            RegistrationActionAuditPhase.POLICY_PRE_MUTATE,
        ):
            if self.outcome is not None:
                raise ValueError("policy audit phases must not carry a source outcome")
            return
        if self.phase is RegistrationActionAuditPhase.SOURCE_RSVP_OUTCOME:
            if self.policy_decision is not RegistrationActionAuditDecision.ALLOWED:
                raise ValueError("a source-RSVP outcome requires an allowed policy decision")
            if self.outcome is None:
                raise ValueError("a source-RSVP audit requires a normalized outcome")
            return
        raise ValueError("registration action-audit phase is invalid")


def _validate_workflow_id(tenant_id: UUID, workflow_id: str) -> None:
    """Keep the stored opaque workflow identity aligned with the deterministic ADR-003 invariant."""
    tenant_text, separator, event_text = workflow_id.partition(":")
    if not separator or tenant_text != str(tenant_id) or ":" in event_text:
        raise ValueError("action audit workflow id must be the deterministic tenant-event id")
    try:
        event_id = UUID(event_text)
    except ValueError as error:
        raise ValueError("action audit workflow id must contain a canonical event UUID") from error
    if workflow_id != f"{tenant_id}:{event_id}":
        raise ValueError("action audit workflow id must be canonically encoded")
