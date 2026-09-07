"""In-memory mock cloud adapters (AWS/Google/Anthropic mocked in the foundation, mock_cloud=True).

These faithfully implement the port Protocols with process-local state so the workflows, API, and
slice tests run with no external cloud dependency. Adapters import domain + ports + stdlib only."""

from __future__ import annotations

from .auth import HeaderAuthContext, LocalHeaderCsrfProtection
from .calendar import MockCalendar
from .calendar_sync import MockGoogleCalendarSyncSink, MockGoogleCalendarSyncState
from .consent import MockRegistrationConsentEvidence
from .email import MockEmailIngestion
from .erasure import MockTenantErasureInventory
from .notifier import MockNotifier
from .object_store import MockFilesystemObjectStore
from .ranking_feedback import InMemoryRankingFeedback
from .relay_inbox import FixtureRelayInboxIngress, RelayExtractorRule, RelayFixtureEnvelope
from .vault import MockInjectionBroker, MockRelaySecretRegistry, MockVault

__all__ = [
    "FixtureRelayInboxIngress",
    "HeaderAuthContext",
    "InMemoryRankingFeedback",
    "LocalHeaderCsrfProtection",
    "MockCalendar",
    "MockEmailIngestion",
    "MockFilesystemObjectStore",
    "MockGoogleCalendarSyncSink",
    "MockGoogleCalendarSyncState",
    "MockInjectionBroker",
    "MockNotifier",
    "MockRegistrationConsentEvidence",
    "MockRelaySecretRegistry",
    "MockTenantErasureInventory",
    "MockVault",
    "RelayExtractorRule",
    "RelayFixtureEnvelope",
]
