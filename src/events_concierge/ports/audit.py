"""Immutable registration-action audit boundary (FR-7.3, NFR-8/10)."""

from __future__ import annotations

from typing import Protocol

from ..domain.audit import RegistrationActionAudit


class RegistrationActionAuditPort(Protocol):
    """Persist one PII-minimized registration fact, converging an exact replay."""

    async def append(self, record: RegistrationActionAudit) -> bool:
        """Return True for a new fact and False for an exact idempotent replay.

        A reused audit key with different immutable fields must fail closed rather than rewrite
        evidence (FR-7.3, NFR-8/10, ADR-003/007).
        """
        ...
