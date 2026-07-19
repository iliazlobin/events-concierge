"""Application settings (pydantic-settings). All values overridable via EC_-prefixed env vars."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EC_", env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "info"

    # Dependency services (docker-compose in local dev).
    database_url: str = "postgresql+psycopg://ec_app:ec_app@localhost:5433/ec"
    redis_url: str = "redis://localhost:6380/0"
    temporal_target: str = "localhost:7234"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "events-concierge"
    # ADR-011 keeps large Temporal payloads in an opaque, tenant-scoped claim-check store.  The
    # local mock is filesystem-backed so independently started API/worker processes share claims.
    claim_check_threshold_bytes: int = Field(default=256 * 1024, gt=0)
    claim_check_local_root: str = "/tmp/events-concierge-claim-check"

    # Discovery -- free-crawl first; API connectors are a future adapter swap.
    discovery_sources: str = "public_jsonld"
    crawl_user_agent: str = "events-concierge-bot/0.1 (+https://example.com/bot)"
    crawl_max_concurrency: int = 4
    crawl_min_interval_ms: int = 1500
    # Empty by default: an owner-approved public seed is required before any live crawl.
    crawl_seed_urls: str = ""
    # A source/time-bucket refresh lease makes manual/worker restarts converge before the future
    # Temporal Schedule layer is enabled (NFR-8).
    catalog_refresh_lease_seconds: int = 300
    # One explicit cadence pass processes only this many currently due reviewed sources. The
    # worker is deliberately one-shot; an operator/deployment scheduler owns recurrence.
    catalog_refresh_dispatch_batch_size: int = Field(default=50, ge=1, le=500)

    # ``auto`` preserves the pre-P1 deployment behavior (Redis whenever cloud mocks are disabled)
    # while an explicit ``redis`` still lets local/integration tests exercise shared state with all
    # cloud adapters mocked. A non-mock deployment must never silently downgrade to process-local
    # full-burst pacing (ADR-005).
    pacer_backend: Literal["auto", "memory", "redis"] = "auto"
    pacer_rate_per_second: float = 5.0
    pacer_burst: int = 10
    pacer_unavailable_retry_seconds: float = 2.0
    # ADR-005 defaults to an independent credential/token bucket for Meetup.  The per-app
    # contingency remains disabled until the owner-run G2 spike has established quota scope and
    # measured request cost; composition rejects an attempted flip without that evidence flag.
    meetup_quota_scope_mode: Literal["per_token", "per_app"] = "per_token"
    meetup_app_quota_scope: str = "meetup-app"
    meetup_app_g2_validated: bool = False
    meetup_app_degrade_after_seconds: float = 300.0
    # Browser capacity is a held admission lease, independent from provider-rate Pacer buckets.
    # The five-minute ceiling is the ratified maximum browser-session wall-clock, and is also the
    # fail-closed Redis-loss recovery fence (AC-45, NFR-4b, ADR-005/006).
    browser_pool_capacity: int = 135
    # Never accept a shorter value: after Redis loss it is the only fence protecting against a
    # still-live pre-loss browser session. A longer value is conservative; FleetPort still must
    # hard-destroy a real session at the ratified five-minute wall-clock cap (ADR-006).
    browser_admission_lease_seconds: float = Field(default=300.0, ge=300.0)
    # Opaque label for the single future Ticketmaster app key; it is not a credential value. It
    # must match the owner-controlled provider_budget_scope row or authorization fails closed.
    ticketmaster_quota_scope: str = "ticketmaster-app"

    # Mock the cloud (KMS/SES/S3/Calendar/Anthropic/Browserbase) in the foundation.
    mock_cloud: bool = True

    # These two provider seams are explicit opt-ins independent of ``mock_cloud``. A configured
    # Cohere key selects its cross-encoder; Google selection additionally requires a tenant-scoped
    # access-token port injected at composition, never a global bearer token (FR-4.2, FR-9.1/9.6).
    cohere_api_key: SecretStr | None = None
    cohere_rerank_model: str = "rerank-v3.5"
    cohere_timeout_seconds: float = Field(default=10.0, gt=0.0)
    google_calendar_enabled: bool = False
    # ``module:callable`` deployment plugin that returns a tenant-scoped GoogleCalendarAccessPort.
    # It is deliberately not a bearer-token setting: OAuth/token lifecycle stays in owner-provided
    # deployment code while this repository retains only the typed port boundary (FR-9.1/9.6).
    google_calendar_access_factory: str | None = None
    google_calendar_timeout_seconds: float = Field(default=10.0, gt=0.0)

    # Policy defaults (owner-accepted, design/owner-decisions.md).
    attempt_budget: int = 3
    handoff_ttl_days: int = 7
    kill_switch: bool = False

    # ADR-009 transactional-outbox relay. A poll is the durable local fallback when PostgreSQL
    # LISTEN/NOTIFY is unavailable or a notifier process reconnects.
    outbox_batch_size: int = 50
    outbox_poll_seconds: float = 2.0
    outbox_lease_seconds: int = 60

    # ADR-003 durable parent-workflow start relay. The API makes a best-effort immediate lease;
    # this separate worker replays any Temporal outage without dropping the accepted request.
    request_start_batch_size: int = 50
    request_start_poll_seconds: float = 2.0
    request_start_lease_seconds: int = 60

    # ADR-008 projects guarded lifecycle entry/exit records into the central watch registry, then
    # fanouts already-recorded organizer changes to Temporal.  This worker has no live detector;
    # provider collection remains separately owner-gated.
    change_delivery_batch_size: int = 50
    change_delivery_poll_seconds: float = 2.0
    change_delivery_lease_seconds: int = 60

    # ADR-007's task-TTL queue only repairs closed/orphaned workflows after a five-minute DB
    # grace; a live Temporal child remains the authority for ordinary expiry.
    handoff_expiry_batch_size: int = 50
    handoff_expiry_poll_seconds: float = 900.0
    handoff_expiry_lease_seconds: int = 60

    # ADR-007/ADR-008 require a nightly, read-only proof that lifecycle rows, Temporal executions,
    # watches, and handoff expiry records still agree. This scanner deliberately has no repair
    # lease: the existing guarded workflow and orphan-repair paths remain the only writers.
    lifecycle_invariant_batch_size: int = Field(default=500, ge=1, le=1000)
    lifecycle_invariant_poll_seconds: float = Field(default=86_400.0, gt=0)

    @property
    def enabled_sources(self) -> list[str]:
        return [s.strip() for s in self.discovery_sources.split(",") if s.strip()]

    @property
    def crawl_seeds(self) -> list[str]:
        """Return explicit, owner-approved public discovery seeds (FR-3.1, FR-10.4)."""
        return [url.strip() for url in self.crawl_seed_urls.split(",") if url.strip()]

    @property
    def uses_shared_pacer_redis(self) -> bool:
        """Whether this settings snapshot selects Redis rather than process-local Pacer state.

        P15a's catalog continuation requires this exact shared posture before its one physical
        LibCal GET.  Keeping the decision on the immutable settings value lets a workflow activity
        fail before egress if its separately deployed worker was configured inconsistently
        (FR-10.4, NFR-8, ADR-005).
        """
        return (
            self.meetup_quota_scope_mode == "per_app"
            or self.pacer_backend == "redis"
            or (self.pacer_backend == "auto" and not self.mock_cloud)
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
