"""In-memory opaque RelayInbox reference channel (FR-5.7/5.8, ADR-011).

The mock models a bounded tenant/source-scoped opaque-reference handoff.  It never stores an OTP,
magic link, raw MIME body, or any other plaintext; its legacy canned-reference seam stays available
for existing workflow tests without needing a live RelayInbox.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from ...domain.enums import Source
from ...ports.email import RelaySecretReference, RelaySecretReferencePublisherPort


def _utc_now() -> datetime:
    """Return the current aware UTC time for expiry checks in the offline channel."""
    return datetime.now(UTC)


class MockEmailIngestion(RelaySecretReferencePublisherPort):
    """Process-local opaque-reference buffer, scoped by tenant and source (ADR-011).

    The published buffer intentionally holds one active capability per scope.  A redelivery of the
    same reference is idempotent; a distinct active reference fails closed rather than overwriting
    an in-flight login.  Consumers dequeue a published reference once, matching a bounded fixture
    handoff while never learning its underlying secret.
    """

    def __init__(
        self,
        canned: RelaySecretReference | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._canned = canned
        self._published: dict[tuple[UUID, Source], RelaySecretReference] = {}
        self._now = now or _utc_now

    def set_secret_reference(self, reference: RelaySecretReference | None) -> None:
        """Set (or clear) the legacy canned opaque reference used by existing workflow fixtures."""
        self._canned = reference

    async def publish_secret_reference(self, reference: RelaySecretReference) -> bool:
        """Buffer one live opaque reference without ever receiving its plaintext (ADR-011)."""
        now = self._now()
        if not _is_aware(now) or reference.expires_at <= now:
            return False

        key = (reference.tenant_id, reference.source)
        existing = self._published.get(key)
        if existing is not None and existing.expires_at <= now:
            del self._published[key]
            existing = None
        if existing is None:
            self._published[key] = reference
            return True
        return existing.secret_ref == reference.secret_ref

    async def await_secret_reference(
        self, tenant_id: UUID, source: Source, timeout_s: float
    ) -> RelaySecretReference | None:
        """Return a scoped opaque reference or None; published values dequeue once (ADR-011)."""
        del timeout_s
        reference = self._canned
        if (
            reference is not None
            and reference.tenant_id == tenant_id
            and reference.source is source
        ):
            return reference

        key = (tenant_id, source)
        published = self._published.get(key)
        if published is None:
            return None
        now = self._now()
        if not _is_aware(now) or published.expires_at <= now:
            del self._published[key]
            return None
        del self._published[key]
        return published


def _is_aware(value: datetime) -> bool:
    """Avoid comparing a malformed fixture clock to a timezone-aware expiry timestamp."""
    return value.tzinfo is not None and value.utcoffset() is not None
