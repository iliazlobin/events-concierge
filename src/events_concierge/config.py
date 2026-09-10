"""Application settings (pydantic-settings). All values overridable via EC_-prefixed env vars."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secret_files import read_secret_file

_MAX_UI_AUTH_URL_LENGTH = 2048
_MAX_OPERATOR_SUBJECT_LENGTH = 200
_MAX_OPERATOR_SUBJECTS = 100
_MIN_PRINTABLE_CODEPOINT = 0x20
_MAX_URL_PORT = 65535
_RELEASE_REVISION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_TEMPORAL_TASK_QUEUE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
_TEMPORAL_WORKER_DEPLOYMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$"
_SECRET_FILE_FIELDS = (
    ("database_url", "database_url_file", "EC_DATABASE_URL"),
    ("operator_database_url", "operator_database_url_file", "EC_OPERATOR_DATABASE_URL"),
    (
        "ingestion_executor_database_url",
        "ingestion_executor_database_url_file",
        "EC_INGESTION_EXECUTOR_DATABASE_URL",
    ),
    ("redis_url", "redis_url_file", "EC_REDIS_URL"),
    ("temporal_api_key", "temporal_api_key_file", "EC_TEMPORAL_API_KEY"),
    ("oidc_client_secret", "oidc_client_secret_file", "EC_OIDC_CLIENT_SECRET"),
    ("cohere_api_key", "cohere_api_key_file", "EC_COHERE_API_KEY"),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EC_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: str = "local"
    log_level: str = "info"
    # Release controller identity exposed to canaries and bounded build metrics. Local defaults are
    # explicit; the production preflight requires an immutable revision and sha256 image digest.
    release_revision: str = Field(default="development", pattern=_RELEASE_REVISION_PATTERN)
    image_digest: str | None = Field(default=None, pattern=_IMAGE_DIGEST_PATTERN)
    # Public origin used in user-facing capability links. Production composition requires HTTPS.
    public_base_url: str = "http://localhost:8000"
    # The local ingestion control room relies on the deployment edge publishing the API only on
    # loopback. Compose passes its actual host-side bind value into this contract.
    api_bind_address: str = "127.0.0.1"
    # Optional deployment/BFF-owned login entrypoint exposed to the static consumer shell. A
    # relative same-origin route is preferred; an absolute value must be HTTPS. The application
    # does not implement OAuth initiation or store a browser token itself.
    ui_auth_start_url: str | None = None

    # Dependency services (docker-compose in local dev).
    database_url: str = "postgresql+psycopg://ec_app:ec_app@localhost:5433/ec"
    database_url_file: str | None = Field(default=None, exclude=True, repr=False)
    # Direct deployments verify the PostgreSQL hostname themselves. Cloud SQL Auth Proxy
    # deployments terminate the authenticated tunnel on loopback and explicitly disable a second
    # TLS layer in the application DSN; production preflight validates the two shapes separately.
    database_connection_mode: Literal["direct_tls", "cloud_sql_proxy", "development_plaintext"] = "direct_tls"
    database_pool_size: int = Field(default=5, ge=1, le=32)
    database_max_overflow: int = Field(default=0, ge=0, le=16)
    database_pool_timeout_seconds: float = Field(default=5.0, ge=0.1, le=30.0)
    database_pool_recycle_seconds: int = Field(default=1_800, ge=30, le=86_400)
    # The catalog's day, topic, city, and provider rollups group tens of thousands of
    # observations. PostgreSQL's 4MB default spilled every one of them to temp files -
    # 68 GB written on this deployment - and the sort finished about a third faster
    # once it had room. This is a per-connection ceiling, not an allocation.
    database_work_mem: str = Field(default="64MB", pattern=r"^\d{1,5}(kB|MB|GB)$")
    redis_url: str = "redis://localhost:6380/0"
    redis_url_file: str | None = Field(default=None, exclude=True, repr=False)
    temporal_target: str = "localhost:7234"
    temporal_namespace: str = "default"
    # ``temporal_task_queue`` is the legacy combined-queue setting retained for local Compose and
    # existing test harnesses. Explicit workload queues fall back to it, so local development keeps
    # one poller while deployed worker processes can isolate user transactions from catalog bursts.
    temporal_task_queue: str = Field(
        default="events-concierge",
        pattern=_TEMPORAL_TASK_QUEUE_PATTERN,
    )
    temporal_transactional_task_queue: str | None = Field(
        default=None,
        pattern=_TEMPORAL_TASK_QUEUE_PATTERN,
    )
    temporal_catalog_task_queue: str | None = Field(
        default=None,
        pattern=_TEMPORAL_TASK_QUEUE_PATTERN,
    )
    # A role-specific worker process lets the deployment scale and restart each queue separately.
    # ``combined`` retains the local entrypoint and, when queues differ, keeps a bounded two-worker
    # fallback for non-orchestrated environments.
    temporal_worker_role: Literal["combined", "transactional", "catalog"] = "combined"
    # Worker Deployments use the immutable release revision unless an equally bounded build ID is
    # supplied explicitly. Versioning is opt-in locally because the pinned development server may
    # not expose the Temporal Cloud Worker Deployment APIs; production validation requires it.
    temporal_worker_deployment_name: str = Field(
        default="events-concierge",
        pattern=_TEMPORAL_WORKER_DEPLOYMENT_PATTERN,
    )
    temporal_worker_build_id: str | None = Field(
        default=None,
        pattern=_RELEASE_REVISION_PATTERN,
    )
    temporal_worker_versioning_enabled: bool = False
    # Temporal Cloud accepts API-key authentication only over TLS. Self-hosted deployments may
    # enable TLS without an API key, while local defaults keep the existing plaintext dev server.
    temporal_tls_enabled: bool = False
    temporal_tls_domain: str | None = None
    temporal_api_key: SecretStr | None = None
    temporal_api_key_file: str | None = Field(default=None, exclude=True, repr=False)
    # Bound every API/worker Temporal RPC independently from workflow execution time. The upper
    # limit prevents a deployment typo from turning durable intake or repair loops into minute-plus
    # socket waits; retryable work remains owned by its PostgreSQL queue.
    temporal_rpc_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    # A combined workflow/activity worker must not inherit the SDK's broad adaptive defaults: one
    # recovered durable backlog can otherwise create hundreds of Python threads and database
    # waiters before a small deployment can apply backpressure. Keep workflow execution bounded by
    # CPU and activities below the per-process SQLAlchemy connection budget; deployments may tune
    # these values only after measuring both resources together.
    # Temporal requires at least two workflow-task slots while its workflow cache is enabled.
    temporal_worker_max_concurrent_workflow_tasks: int = Field(default=8, ge=2, le=64)
    temporal_worker_max_concurrent_activities: int = Field(default=8, ge=1, le=64)
    # Cap the complete inbound body-read interval so a slow/dripping client cannot retain an API
    # request task indefinitely while remaining under the decoded-byte limit.
    request_body_timeout_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    # ADR-011 keeps large Temporal payloads in an opaque, tenant-scoped claim-check store.  The
    # local mock is filesystem-backed so independently started API/worker processes share claims.
    claim_check_threshold_bytes: int = Field(default=256 * 1024, gt=0)
    claim_check_local_root: str = "/tmp/events-concierge-claim-check"
    # Profile media is durable, unlike claim checks, so it gets its own store and its own root.
    # Pointing this at a mounted volume is all a deployment needs to do until blob storage lands.
    media_local_root: str = "/var/lib/events-concierge/media"
    gcp_project: str | None = None
    gcs_claim_check_bucket: str | None = None
    # Explicit catalog executors override this to a distinct events-concierge/catalog/ prefix;
    # their converter and Workload Identity cannot retrieve consumer tenant payloads.
    gcs_claim_check_prefix: str = "events-concierge/claim-check/v1"

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
    # The ingestion control room is a deliberately local-only operational surface until a
    # deployment provides a distinct operator identity/session and database role.  Consumer
    # tenant authentication is never treated as administration authority.
    admin_ingestion_enabled: bool = False
    # Hosted operators run a separate entrypoint, identity verifier, and database pool. These
    # settings never install operator routes into the consumer app.
    operator_api_enabled: bool = False
    operator_iap_audience: str | None = Field(default=None, min_length=1, max_length=512)
    operator_public_origin: str | None = None
    operator_subject_roles: dict[str, Literal["viewer", "operator", "reviewer"]] = Field(
        default_factory=dict,
        repr=False,
    )
    operator_database_url: str | None = Field(default=None, exclude=True, repr=False)
    operator_database_url_file: str | None = Field(default=None, exclude=True, repr=False)
    ingestion_executor_enabled: bool = False
    ingestion_executor_database_url: str | None = Field(default=None, exclude=True, repr=False)
    ingestion_executor_database_url_file: str | None = Field(default=None, exclude=True, repr=False)
    # The local profile runs a loop; production runs this enqueue-only worker with --once from
    # the deployment scheduler. Both persist the same deterministic command receipts and perform
    # no provider I/O. Recurrence never depends on whether the hosted operator UI is enabled.
    catalog_ingestion_scheduler_enabled: bool = False
    catalog_ingestion_scheduler_interval_seconds: int = Field(
        default=300,
        ge=60,
        le=3_600,
    )
    catalog_ingestion_command_batch_size: int = Field(default=2, ge=1, le=10)
    catalog_ingestion_command_poll_seconds: float = Field(default=2.0, ge=0.25, le=60.0)
    # A bounded cadence command may sequentially dispatch several reviewed sources. The worker
    # heartbeats this short recovery lease while its guarded router is active, so a healthy worker
    # is not reclaimed and a crashed process does not strand the fleet slot for hours.
    catalog_ingestion_command_lease_seconds: int = Field(
        default=300,
        ge=300,
        le=21_600,
    )
    # Public entity intelligence starts only from exact source profile URLs.  The separate worker
    # keeps egress bounded and can be disabled independently from event ingestion.
    entity_intelligence_enabled: bool = False
    entity_intelligence_batch_size: int = Field(default=2, ge=1, le=10)
    entity_intelligence_poll_seconds: float = Field(default=30.0, ge=5.0, le=3_600.0)

    # ``auto`` preserves the pre-P1 deployment behavior (Redis whenever cloud mocks are disabled)
    # while an explicit ``redis`` still lets local/integration tests exercise shared state with all
    # cloud adapters mocked. A non-mock deployment must never silently downgrade to process-local
    # full-burst pacing (ADR-005).
    pacer_backend: Literal["auto", "memory", "redis"] = "auto"
    pacer_rate_per_second: float = 5.0
    pacer_burst: int = 10
    pacer_unavailable_retry_seconds: float = 2.0
    # How long the shared Redis Pacer keeps a bucket's tokens/timestamp. A missing bucket is
    # deliberately treated as lost state and fails throttle-first (ADR-005), so this must exceed
    # the gap between two ordinary uses of one bucket. Reviewed sources refresh on cadences up to
    # ``MAX_SOURCE_REFRESH_INTERVAL_MINUTES`` (one day); at the previous one-refill-window value every one
    # of them found its bucket expired on every attempt and was deferred before any provider call.
    pacer_state_retention_seconds: float = Field(default=604_800.0, ge=1.0, le=2_592_000.0)
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
    # ``module:callable`` deployment hook. The callable receives this immutable settings snapshot
    # and returns ``RuntimePorts``; secrets, browser-session authentication, its matching
    # CsrfProtectionPort, and concrete cloud SDKs remain in deployment-owned code.
    runtime_provider_factory: str | None = None

    # Built-in production browser identity boundary. This is an authorization-code OIDC BFF:
    # identity/access tokens never enter browser storage, while opaque login and session handles
    # live in Secure __Host- cookies and resolve through the shared Redis control plane.
    oidc_bff_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_authorization_url: str | None = None
    oidc_token_url: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_client_secret_file: str | None = Field(default=None, exclude=True, repr=False)
    oidc_tenant_claim: str | None = None
    oidc_algorithms: str = "RS256"
    oidc_login_ttl_seconds: int = Field(default=600, ge=60, le=900)
    oidc_session_ttl_seconds: int = Field(default=28_800, ge=300, le=86_400)
    # Destructive account erasure requires a provider-verified ``auth_time`` inside this window.
    # The dedicated /auth/reauth path uses prompt=login + max_age=0 before rotating the session.
    account_erasure_recent_auth_seconds: int = Field(default=300, ge=60, le=900)
    oidc_http_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    oidc_session_store_timeout_seconds: float = Field(default=2.0, gt=0.0, le=10.0)

    # These two provider seams are explicit opt-ins independent of ``mock_cloud``. A configured
    # Cohere key selects its cross-encoder; Google selection additionally requires a tenant-scoped
    # access-token port injected at composition, never a global bearer token (FR-4.2, FR-9.1/9.6).
    # Conversational agent surface. A third explicit provider seam, independent of mock_cloud:
    # a deployment opts in deliberately rather than inheriting an outbound model call.
    agent_enabled: bool = False

    cohere_api_key: SecretStr | None = None
    cohere_api_key_file: str | None = Field(default=None, exclude=True, repr=False)
    cohere_rerank_model: str = "rerank-v3.5"
    cohere_timeout_seconds: float = Field(default=10.0, gt=0.0)
    google_calendar_enabled: bool = False
    # ``module:callable`` deployment plugin that returns a tenant-scoped GoogleCalendarAccessPort.
    # It is deliberately not a bearer-token setting: OAuth/token lifecycle stays in owner-provided
    # deployment code while this repository retains only the typed port boundary (FR-9.1/9.6).
    google_calendar_access_factory: str | None = None
    google_calendar_timeout_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    tenant_effect_timeout_seconds: float = Field(default=30.0, ge=0.1, le=60.0)
    tenant_effect_lock_timeout_seconds: float = Field(default=5.0, ge=0.1, le=30.0)

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
    # The poll interval is also the minimum interval between non-empty relay passes. Without that
    # bound a recovered queue loops at CPU speed and amplifies each parent into registration
    # children faster than the workflow/activity worker can admit them.
    request_start_batch_size: int = Field(default=5, ge=1, le=50)
    request_start_poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    request_start_lease_seconds: int = Field(default=60, ge=1, le=3600)

    # FR-10.5 erasure is initiated by an authenticated browser command but completed by a
    # durable cross-tenant relay. A short initial delay lets the request path finish immediately;
    # the worker then resumes every pending tombstone after crashes or provider outages.
    account_erasure_batch_size: int = Field(default=10, ge=1, le=100)
    account_erasure_poll_seconds: float = Field(default=30.0, ge=0.5, le=300.0)
    account_erasure_lease_seconds: int = Field(default=120, ge=5, le=3600)

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
    # lease: the existing guarded workflow and orphan-repair paths remain the only writers. Smooth
    # Temporal describes at a conservative per-process rate so an inventory scan cannot starve
    # workflow traffic or health probes.
    lifecycle_invariant_batch_size: int = Field(default=500, ge=1, le=1000)
    lifecycle_invariant_liveness_calls_per_second: float = Field(default=10.0, ge=1.0, le=20.0)
    lifecycle_invariant_poll_seconds: float = Field(default=86_400.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def resolve_secret_file_settings(cls, data: Any) -> Any:
        """Resolve mounted secrets once, rejecting ambiguous inline/file configuration."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        for target, file_field, env_name in _SECRET_FILE_FIELDS:
            file_value = values.get(file_field)
            if file_value is None:
                continue
            if not isinstance(file_value, str) or not file_value:
                raise ValueError(f"{env_name}_FILE must be a non-empty absolute path")
            inline_value = values.get(target)
            if inline_value is not None:
                raise ValueError(f"configure exactly one of {env_name} and {env_name}_FILE")
            values[target] = read_secret_file(file_value, setting_name=env_name)
        return values

    @field_validator("ui_auth_start_url")
    @classmethod
    def validate_ui_auth_start_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > _MAX_UI_AUTH_URL_LENGTH
            or "\\" in value
            or any(ord(char) < _MIN_PRINTABLE_CODEPOINT for char in value)
        ):
            raise ValueError("UI auth start URL must be a bounded, unambiguous printable value")
        if value.startswith("/") and not value.startswith("//"):
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError("UI auth start URL is malformed") from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= _MAX_URL_PORT)
        ):
            raise ValueError("UI auth start URL must be a relative path or an HTTPS URL")
        return value

    @model_validator(mode="after")
    def validate_operator_configuration(self) -> Settings:
        """Keep a partially provisioned operator process from accepting any request."""
        if (
            any(
                not subject
                or len(subject) > _MAX_OPERATOR_SUBJECT_LENGTH
                or not subject.isprintable()
                or any(char.isspace() for char in subject)
                for subject in self.operator_subject_roles
            )
            or len(self.operator_subject_roles) > _MAX_OPERATOR_SUBJECTS
        ):
            raise ValueError("operator subjects must be a bounded explicit identity allowlist")
        if self.operator_api_enabled:
            if self.mock_cloud or self.admin_ingestion_enabled:
                raise ValueError("hosted operator API cannot use the local mock admin profile")
            if not self.operator_iap_audience or not self.operator_subject_roles:
                raise ValueError("operator API requires IAP audience and explicit subject roles")
            if not self.operator_database_url:
                raise ValueError("operator API requires a separate operator database credential")
            origin = urlsplit(self.operator_public_origin or "")
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
            ):
                raise ValueError(
                    "operator API requires an exact HTTPS public origin without a path"
                )
        if self.ingestion_executor_enabled and not self.ingestion_executor_database_url:
            raise ValueError("ingestion executor requires a separate database credential")
        return self

    @model_validator(mode="after")
    def validate_development_database(self) -> Settings:
        if self.database_connection_mode == "development_plaintext" and (
            self.env != "development" or not self.mock_cloud
        ):
            raise ValueError("plaintext database is restricted to explicit development test integrations")
        return self

    @model_validator(mode="after")
    def validate_oidc_bff_configuration(self) -> Settings:
        """Reject partial or local BFF activation before any listener can become ready."""
        if self.catalog_ingestion_scheduler_enabled:
            if self.mock_cloud and not self.admin_ingestion_enabled:
                raise ValueError("local ingestion cadence scheduler requires local ingestion admin")
            if not self.mock_cloud and not self.operator_database_url:
                raise ValueError(
                    "production cadence requires a separate controller database credential"
                )
        if self.admin_ingestion_enabled and not self.mock_cloud:
            raise ValueError(
                "ingestion admin is local-only until a production operator identity is configured"
            )
        if self.admin_ingestion_enabled and not _is_loopback_address(self.api_bind_address):
            raise ValueError("ingestion admin requires a loopback-only API bind address")
        if not self.oidc_bff_enabled:
            return self
        if self.mock_cloud:
            raise ValueError("OIDC BFF sessions cannot be enabled in mock-cloud mode")
        if self.ui_auth_start_url not in {None, "/auth/login"}:
            raise ValueError("OIDC BFF UI auth start URL must be the same-origin /auth/login route")
        required = {
            "oidc_issuer": self.oidc_issuer,
            "oidc_authorization_url": self.oidc_authorization_url,
            "oidc_token_url": self.oidc_token_url,
            "oidc_jwks_url": self.oidc_jwks_url,
            "oidc_client_id": self.oidc_client_id,
            "oidc_tenant_claim": self.oidc_tenant_claim,
        }
        missing = sorted(name for name, value in required.items() if not value or not value.strip())
        if missing:
            raise ValueError(f"OIDC BFF configuration is missing: {', '.join(missing)}")
        if (
            self.oidc_client_secret is None
            or not self.oidc_client_secret.get_secret_value().strip()
        ):
            raise ValueError("OIDC BFF configuration requires oidc_client_secret")
        if not self.oidc_algorithm_allowlist:
            raise ValueError("OIDC algorithms must contain at least one configured value")
        return self

    @property
    def enabled_sources(self) -> list[str]:
        return [s.strip() for s in self.discovery_sources.split(",") if s.strip()]

    @property
    def oidc_algorithm_allowlist(self) -> tuple[str, ...]:
        """Return the explicit signing-algorithm allowlist; the JWT header never selects it."""
        return tuple(value.strip() for value in self.oidc_algorithms.split(",") if value.strip())

    @property
    def crawl_seeds(self) -> list[str]:
        """Return explicit, owner-approved public discovery seeds (FR-3.1, FR-10.4)."""
        return [url.strip() for url in self.crawl_seed_urls.split(",") if url.strip()]

    @property
    def temporal_transactional_queue(self) -> str:
        """Resolve the user request/registration queue with the legacy local fallback."""
        return self.temporal_transactional_task_queue or self.temporal_task_queue

    @property
    def temporal_catalog_queue(self) -> str:
        """Resolve the catalog continuation queue with the legacy local fallback."""
        return self.temporal_catalog_task_queue or self.temporal_task_queue

    @property
    def temporal_effective_worker_build_id(self) -> str:
        """Return the immutable deployment build identity, or the explicit local revision."""
        return self.temporal_worker_build_id or self.release_revision

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


def _is_loopback_address(value: str) -> bool:
    """Accept only an explicit loopback IP or localhost for the local admin edge."""
    normalized = value.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
