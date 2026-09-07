"""PII-free account-erasure command and progress vocabulary (FR-10.5, NFR-10/11).

The durable database row deliberately keeps only opaque command identity, fixed stage counters,
and timestamps. Event titles, request text, provider errors, credentials, addresses, and remote
artifact identifiers never enter this projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class AccountErasureStatus(StrEnum):
    """Durable lifecycle of one tenant's single account-erasure command."""

    ERASING = "erasing"
    COMPLETED = "completed"


class AccountErasureStage(StrEnum):
    """Replay-safe external stages completed before irreversible database deletion."""

    EXTERNAL_EFFECTS = "external_effects"
    WORKFLOWS = "workflows"
    CALENDAR = "calendar"
    BROWSER_SESSIONS = "browser_sessions"
    CREDENTIAL_VAULT = "credential_vault"
    OBJECT_STORE = "object_store"


class AccountErasureFailureStage(StrEnum):
    """Bounded retry reason retained by the worker; never contains provider error text."""

    EXTERNAL_EFFECTS = "external_effects"
    WORKFLOWS = "workflows"
    CALENDAR = "calendar"
    BROWSER_SESSIONS = "browser_sessions"
    CREDENTIAL_VAULT = "credential_vault"
    OBJECT_STORE = "object_store"
    DATABASE = "database"


@dataclass(frozen=True, slots=True)
class AccountErasureSnapshot:
    """One PII-free durable command snapshot plus its current opaque work inventory."""

    tenant_id: UUID
    request_id: UUID
    status: AccountErasureStatus
    workflow_ids: tuple[str, ...]
    canonical_event_ids: tuple[UUID, ...]
    workflow_target_count: int
    calendar_target_count: int
    calendar_binding_expected: bool
    external_effects_completed: bool
    workflows_completed: bool
    calendar_completed: bool
    browser_sessions_completed: bool
    credential_vault_completed: bool
    object_store_completed: bool
    retained_audit_rows: int
    last_failure_stage: AccountErasureFailureStage | None = None

    @property
    def external_stages_completed(self) -> bool:
        """Return true only after every destructive external family has converged."""
        return all(
            (
                self.workflows_completed,
                self.external_effects_completed,
                self.calendar_completed,
                self.browser_sessions_completed,
                self.credential_vault_completed,
                self.object_store_completed,
            )
        )


@dataclass(frozen=True, slots=True)
class AccountErasureLease:
    """Opaque system-worker lease for one already-authenticated erasure command."""

    tenant_id: UUID
    request_id: UUID
    attempt_count: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class AccountErasureResult:
    """Safe API/application result; it intentionally contains no tenant or artifact identity."""

    request_id: UUID
    status: AccountErasureStatus
    workflow_targets: int
    workflows_cancelled: int
    calendar_targets: int
    calendar_deleted: int
    browser_sessions_revoked: bool
    credential_vault_purged: bool
    object_store_purged: bool
    external_effects_drained: bool
    retained_audit_rows: int
    failed_stage: AccountErasureStage | None = None
