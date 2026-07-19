"""In-memory immutable registration-action audit double (FR-7.3, NFR-8/10)."""

from __future__ import annotations

from ...domain.audit import RegistrationActionAudit


class MockRegistrationActionAudit:
    """Faithful replay/immutability double for unit tests and offline composition overrides."""

    def __init__(self) -> None:
        self.records: list[RegistrationActionAudit] = []
        self._by_key: dict[str, RegistrationActionAudit] = {}

    async def append(self, record: RegistrationActionAudit) -> bool:
        existing = self._by_key.get(record.audit_key)
        if existing is None:
            self._by_key[record.audit_key] = record
            self.records.append(record)
            return True
        if existing != record:
            raise ValueError("action audit key is already bound to a different immutable fact")
        return False
