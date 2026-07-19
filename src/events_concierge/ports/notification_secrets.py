"""Protection boundary for short-lived secrets carried by notification projections.

The transactional outbox is a global relay queue rather than a tenant-RLS data surface.  A
user-facing bearer capability must therefore be authenticated-encrypted before it enters an
outbox payload and revealed only by the notifier process immediately before delivery.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class NotificationSecretProtectionError(RuntimeError):
    """A protected notification value was malformed, tampered with, or bound to another tenant."""


class NotificationSecretProtector(Protocol):
    """Protect and reveal the handoff completion URL using tenant-bound authenticated encryption."""

    async def protect_completion_url(self, tenant_id: UUID, completion_url: str) -> str:
        """Return an opaque projection safe to persist in the global notification outbox."""
        ...

    async def reveal_completion_url(self, tenant_id: UUID, protected_value: str) -> str:
        """Reveal only a valid projection authenticated for this exact tenant."""
        ...
