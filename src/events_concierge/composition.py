"""Composition root: the single place that imports concrete adapters and wires them into services.

Everything else depends on ports; only this module knows the concrete graph. Local/mock composition
uses deterministic ranking doubles and ``MockCalendar``. An explicit Cohere key selects the real
cross-encoder; an explicit Google flag plus an injected tenant-scoped access port selects the
Google ``CalendarPort``. Both defaults remain offline, and OAuth/token provisioning stays outside
the foundation (FR-4.2/FR-4.3/FR-9.1/9.6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import cast

import httpx

from .adapters.berkeley_rep.source import BerkeleyRepCatalogFetcher
from .adapters.calperformances.source import CalPerformancesCatalogFetcher
from .adapters.entity_intelligence.public_sources import (
    GitHubOrganizationSource,
    OfficialWebsiteSource,
    SafePublicHttpClient,
    WikidataSource,
)
from .adapters.google_calendar.calendar import GoogleCalendarAdapter
from .adapters.identity_platform import (
    IdentityPlatformBrowserSessionAdapter,
    IdentityPlatformVerifier,
)
from .adapters.local_media import LocalFilesystemMediaStore
from .adapters.luma.source import LumaSource
from .adapters.mock.auth import HeaderAuthContext, LocalHeaderCsrfProtection
from .adapters.mock.calendar import MockCalendar
from .adapters.mock.notification_secrets import DevelopmentNotificationSecretProtector
from .adapters.mock.notifier import MockNotifier
from .adapters.mock.object_store import MockFilesystemObjectStore
from .adapters.mock.vault import MockVault
from .adapters.oidc.session import OidcBffSessionAdapter
from .adapters.policy.browser_admission import InMemoryBrowserAdmission, RedisBrowserAdmission
from .adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from .adapters.policy.engine import StoreBackedPolicyEngine
from .adapters.policy.pacer import InMemoryPacer, RedisPacer, default_source_budgets
from .adapters.postgres.account_erasure import PostgresAccountErasureRepository
from .adapters.postgres.api_keys import PostgresApiKeyRepository
from .adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from .adapters.postgres.budget import PostgresBudgetLedger
from .adapters.postgres.calendar_bindings import PostgresGoogleCalendarBindings
from .adapters.postgres.calendar_repair import PostgresClosedWorkflowCalendarRepairRepository
from .adapters.postgres.catalog import PostgresCatalogRepository
from .adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from .adapters.postgres.catalog_entity_social_links import PostgresCatalogEntitySocialLinks
from .adapters.postgres.catalog_observations import PostgresCatalogObservationRepository
from .adapters.postgres.catalog_paged_promotion import PostgresCatalogPagedRefreshPromoter
from .adapters.postgres.catalog_refresh_commit import PostgresCatalogRefreshCommitter
from .adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from .adapters.postgres.change_detection import PostgresChangeDetectionRepository
from .adapters.postgres.consent import PostgresRegistrationConsentEvidenceRepository
from .adapters.postgres.consumer import PostgresConsumerReadRepository
from .adapters.postgres.consumer_accounts import PostgresConsumerAccountRepository
from .adapters.postgres.discovery_policy import PostgresDiscoveryPolicyReader
from .adapters.postgres.handoff_expiry import PostgresHandoffExpiryRepository
from .adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from .adapters.postgres.invariants import PostgresLifecycleInvariantRepository
from .adapters.postgres.muse import PostgresMuseRepository
from .adapters.postgres.outbox_wakeup import PostgresOutboxWakeup
from .adapters.postgres.policy import (
    PostgresPolicySnapshotRepository,
    PostgresSourceQuarantineRepository,
)
from .adapters.postgres.profile_avatar import PostgresProfileAvatarRepository
from .adapters.postgres.profile_media import PostgresProfileMediaMutationGuard
from .adapters.postgres.ranking import PostgresRankingProfileRepository
from .adapters.postgres.ranking_feedback import PostgresRankingFeedbackRepository
from .adapters.postgres.saved_catalog_filters import PostgresSavedCatalogFilterRepository
from .adapters.postgres.tenant_effects import PostgresTenantEffectAuthority
from .adapters.postgres.tenant_profile import PostgresTenantProfileRepository
from .adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
    PostgresOutboxRepository,
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from .adapters.postgres.tenant_roles import PostgresTenantRoleRepository
from .adapters.postgres.watch_projection import PostgresLifecycleWatchProjectionOutbox
from .adapters.ranking.cohere import CohereRerankCrossEncoder
from .adapters.ranking.embedding import DeterministicEmbedding
from .adapters.ranking.ranker import (
    DeterministicCrossEncoder,
    DeterministicFeatureRescorer,
    PersonalizedRanker,
)
from .adapters.usfca.source import UsfcaCatalogFetcher
from .adapters.ybca.source import YbcaCatalogFetcher
from .application.catalog_execution_descriptors import CatalogExecutionDescriptorRegistry
from .application.catalog_paged_refresh import PagedCatalogRefreshService
from .application.catalog_refresh import CatalogRefreshService
from .application.discovery import DiscoveryService
from .application.entity_intelligence import EntityIntelligenceService
from .application.feed import FeedService, MembershipResolver
from .application.handoff_reminder import HandoffReminderService
from .application.muse import MuseSignupService
from .application.parsing import HeuristicRequestParser
from .application.ranking_feedback import FeedbackAwareRankingProfiles, RankingFeedbackService
from .application.reconciliation import LifecycleReconciliationService
from .application.registration import RegistrationService
from .application.request_terminal import RequestTerminalService
from .application.ticketmaster_budget import TicketmasterDispatchGate
from .config import Settings, get_settings
from .deployment.discovery_runtime import discovery_effects_disabled, validate_discovery_settings
from .deployment.gcp_runtime import build_gcs_media_store
from .domain.enums import CatalogSourceMode, Source
from .domain.policy import SourcePolicy
from .infra.db import init_engine
from .policies import default_source_policies
from .ports.account_erasure import AccountErasureRepository
from .ports.api_keys import ApiKeyRepository
from .ports.audit import RegistrationActionAuditPort
from .ports.auth import AuthContextPort, BrowserSessionLifecyclePort, CsrfProtectionPort
from .ports.browser_admission import BrowserAdmissionPort
from .ports.calendar import CalendarPort
from .ports.catalog_sources import CatalogPagedSourceFetcher, CatalogSourceFetcher
from .ports.consent import RegistrationConsentEvidencePort
from .ports.consumer import ConsumerReadPort
from .ports.consumer_accounts import ConsumerAccountRepository
from .ports.credentials import CredentialVault
from .ports.discovery_policy import DiscoveryPolicyGate
from .ports.google_calendar import GoogleCalendarAccessPort, GoogleCalendarBindingPort
from .ports.invariants import LifecycleInvariantRepository
from .ports.media_store import MediaStorePort
from .ports.notification_secrets import NotificationSecretProtector
from .ports.notifications import NotificationPort
from .ports.object_store import ObjectStorePort
from .ports.outbox import OutboxWakeupPort
from .ports.policy import Pacer, PolicyEngine, SourceQuarantinePort
from .ports.profile_avatar import ProfileAvatarRepository, ProfileMediaMutationGuard
from .ports.ranking import RankerPort, RankingProfileRepository
from .ports.ranking_feedback import RankingFeedbackRepository
from .ports.saved_catalog_filters import SavedCatalogFilterRepository
from .ports.sources import SourcePort
from .ports.tenant_effects import TenantEffectAuthority, TenantEffectAuthorityConfig
from .ports.tenant_profile import TenantProfileRepository
from .ports.tenant_roles import TenantRoleRepository
from .ports.withdrawal import RegistrationWithdrawalPort
from .runtime import RuntimePorts, load_runtime_ports


@dataclass(slots=True)
class Container:
    settings: Settings
    embedding: DeterministicEmbedding
    catalog: PostgresCatalogRepository
    catalog_entities: PostgresCatalogEntityRepository
    entity_intelligence: EntityIntelligenceService
    catalog_observation_repo: PostgresCatalogObservationRepository
    catalog_source_repo: PostgresCatalogSourceRepository
    ingestion_admin_repo: PostgresIngestionAdminRepository
    budget_ledger: PostgresBudgetLedger
    action_audit: RegistrationActionAuditPort
    account_erasure_repo: AccountErasureRepository
    consumer_accounts: ConsumerAccountRepository
    tenant_repo: PostgresTenantRepository
    consumer: ConsumerReadPort
    request_repo: PostgresRequestRepository
    lifecycle_repo: PostgresLifecycleRepository
    handoff_repo: PostgresHandoffRepository
    handoff_expiry_repo: PostgresHandoffExpiryRepository
    lifecycle_invariant_repo: LifecycleInvariantRepository
    outbox_repo: PostgresOutboxRepository
    outbox_wakeup: OutboxWakeupPort
    change_detection_repo: PostgresChangeDetectionRepository
    watch_projection_outbox: PostgresLifecycleWatchProjectionOutbox
    calendar_repair_repo: PostgresClosedWorkflowCalendarRepairRepository
    ranking_profiles: RankingProfileRepository
    tenant_profiles: TenantProfileRepository
    profile_avatars: ProfileAvatarRepository
    media_store: MediaStorePort
    profile_media_mutations: ProfileMediaMutationGuard
    tenant_roles: TenantRoleRepository
    api_keys: ApiKeyRepository
    saved_catalog_filters: SavedCatalogFilterRepository
    ranking_feedback_repo: RankingFeedbackRepository
    ranking_feedback: RankingFeedbackService
    ranker: RankerPort
    calendar: CalendarPort
    notifier: NotificationPort
    notification_secret_protector: NotificationSecretProtector
    vault: CredentialVault
    registration_consent: RegistrationConsentEvidencePort
    policy: PolicyEngine
    source_quarantine: SourceQuarantinePort
    discovery_policy_gate: DiscoveryPolicyGate
    pacer: Pacer
    browser_admission: BrowserAdmissionPort
    object_store: ObjectStorePort
    auth_context: AuthContextPort
    csrf_protection: CsrfProtectionPort
    browser_session: BrowserSessionLifecyclePort | None
    parser: HeuristicRequestParser
    discovery: DiscoveryService
    catalog_refresh: CatalogRefreshService
    catalog_paged_refresh: PagedCatalogRefreshService
    ticketmaster_budget: TicketmasterDispatchGate
    feed: FeedService
    registration: RegistrationService
    reconciliation: LifecycleReconciliationService
    handoff_reminders: HandoffReminderService
    request_terminal: RequestTerminalService
    source_policies: dict[Source, SourcePolicy]
    tenant_effect_authority: TenantEffectAuthority
    muse: MuseSignupService | None = None


def build_container(
    settings: Settings | None = None,
    *,
    discovery_sources: list[SourcePort] | None = None,
    register_sources: dict[Source, SourcePort] | None = None,
    withdrawal_sources: dict[Source, RegistrationWithdrawalPort] | None = None,
    membership_resolver: MembershipResolver | None = None,
    pacer: Pacer | None = None,
    browser_admission: BrowserAdmissionPort | None = None,
    outbox_wakeup: OutboxWakeupPort | None = None,
    ranking_profiles: RankingProfileRepository | None = None,
    tenant_profiles: TenantProfileRepository | None = None,
    profile_avatars: ProfileAvatarRepository | None = None,
    media_store: MediaStorePort | None = None,
    profile_media_mutations: ProfileMediaMutationGuard | None = None,
    tenant_roles: TenantRoleRepository | None = None,
    api_keys: ApiKeyRepository | None = None,
    saved_catalog_filters: SavedCatalogFilterRepository | None = None,
    ranking_feedback_repo: RankingFeedbackRepository | None = None,
    ranker: RankerPort | None = None,
    cohere_client: httpx.AsyncClient | None = None,
    calendar: CalendarPort | None = None,
    google_calendar_access: GoogleCalendarAccessPort | None = None,
    google_calendar_bindings: GoogleCalendarBindingPort | None = None,
    google_calendar_client: httpx.AsyncClient | None = None,
    object_store: ObjectStorePort | None = None,
    auth_context: AuthContextPort | None = None,
    csrf_protection: CsrfProtectionPort | None = None,
    browser_session: BrowserSessionLifecyclePort | None = None,
    action_audit: RegistrationActionAuditPort | None = None,
    registration_consent: RegistrationConsentEvidencePort | None = None,
    policy_engine: PolicyEngine | None = None,
    discovery_policy_gate: DiscoveryPolicyGate | None = None,
    source_quarantine: SourceQuarantinePort | None = None,
    notifier: NotificationPort | None = None,
    notification_secret_protector: NotificationSecretProtector | None = None,
    credential_vault: CredentialVault | None = None,
    runtime_ports: RuntimePorts | None = None,
) -> Container:
    """Build the graph, accepting provisioned boundary overrides at the composition root."""
    settings = settings or get_settings()
    provisioned = runtime_ports if runtime_ports is not None else load_runtime_ports(settings)
    discovery_sources = (
        discovery_sources
        if discovery_sources is not None
        else (
            list(provisioned.discovery_sources)
            if provisioned.discovery_sources is not None
            else None
        )
    )
    register_sources = (
        register_sources
        if register_sources is not None
        else (
            dict(provisioned.register_sources) if provisioned.register_sources is not None else None
        )
    )
    withdrawal_sources = (
        withdrawal_sources
        if withdrawal_sources is not None
        else (
            dict(provisioned.withdrawal_sources)
            if provisioned.withdrawal_sources is not None
            else None
        )
    )
    # This injection remains an offline/test seam only. A Source-only resolver cannot establish
    # the tenant-and-event-specific Meetup fact required by production routing, so RuntimePorts is
    # intentionally unable to provision one. The execution workflow performs the fresh guarded
    # read through RegistrationService before any autonomous mutation.
    calendar = calendar if calendar is not None else provisioned.calendar
    google_calendar_access = (
        google_calendar_access
        if google_calendar_access is not None
        else provisioned.google_calendar_access
    )
    google_calendar_bindings = (
        google_calendar_bindings
        if google_calendar_bindings is not None
        else provisioned.google_calendar_bindings
    )
    object_store = object_store if object_store is not None else provisioned.object_store
    auth_context = auth_context if auth_context is not None else provisioned.auth_context
    csrf_protection = (
        csrf_protection if csrf_protection is not None else provisioned.csrf_protection
    )
    browser_session = (
        browser_session if browser_session is not None else provisioned.browser_session
    )
    notifier = notifier if notifier is not None else provisioned.notifier
    notification_secret_protector = (
        notification_secret_protector
        if notification_secret_protector is not None
        else provisioned.notification_secret_protector
    )
    credential_vault = (
        credential_vault if credential_vault is not None else provisioned.credential_vault
    )
    action_audit = action_audit if action_audit is not None else provisioned.action_audit
    registration_consent = (
        registration_consent
        if registration_consent is not None
        else provisioned.registration_consent
    )
    if settings.release_profile == "discovery" and not settings.mock_cloud:
        validate_discovery_settings(settings)
        if not (settings.oidc_bff_enabled or settings.identity_platform_enabled) or any(
            boundary is not None for boundary in (auth_context, csrf_protection, browser_session)
        ):
            raise ValueError(
                "non-mock discovery requires the configured browser session as sole identity authority"
            )
        if not discovery_effects_disabled(
            RuntimePorts(
                notifier=notifier,
                notification_secret_protector=notification_secret_protector,
                credential_vault=credential_vault,
                calendar=calendar,
                google_calendar_access=google_calendar_access,
                google_calendar_bindings=google_calendar_bindings,
                register_sources=register_sources,
                withdrawal_sources=withdrawal_sources,
            )
        ):
            raise ValueError(
                "non-mock discovery requires explicit disabled product ports and empty mutation maps"
            )
    init_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        work_mem=settings.database_work_mem,
    )
    tenant_effect_authority = PostgresTenantEffectAuthority(
        TenantEffectAuthorityConfig(
            lock_timeout_seconds=settings.tenant_effect_lock_timeout_seconds
        )
    )

    embedding = DeterministicEmbedding()
    catalog = PostgresCatalogRepository(embedding)
    catalog_entities = PostgresCatalogEntityRepository()
    entity_source_http = SafePublicHttpClient(user_agent=settings.crawl_user_agent)
    entity_intelligence = EntityIntelligenceService(
        catalog_entities,
        official_website=OfficialWebsiteSource(entity_source_http),
        github=GitHubOrganizationSource(entity_source_http),
        wikidata=WikidataSource(entity_source_http),
    )
    catalog_observation_repo, catalog_source_repo = (
        PostgresCatalogObservationRepository(),
        PostgresCatalogSourceRepository(),
    )
    ingestion_admin_repo = PostgresIngestionAdminRepository(
        CatalogExecutionDescriptorRegistry(settings.temporal_task_queue)
    )
    catalog_paged_promoter = PostgresCatalogPagedRefreshPromoter(catalog, catalog_observation_repo)
    catalog_refresh_committer = PostgresCatalogRefreshCommitter(
        catalog, catalog_observation_repo, PostgresCatalogEntitySocialLinks()
    )
    tenant_repo, request_repo, lifecycle_repo, handoff_repo, outbox_repo = (
        PostgresTenantRepository(),
        PostgresRequestRepository(),
        PostgresLifecycleRepository(),
        PostgresHandoffRepository(),
        PostgresOutboxRepository(),
    )
    consumer = PostgresConsumerReadRepository()
    handoff_expiry_repo, lifecycle_invariant_repo = (
        PostgresHandoffExpiryRepository(),
        PostgresLifecycleInvariantRepository(),
    )
    change_detection_repo, watch_projection_outbox, calendar_repair_repo = (
        _build_change_delivery_repositories()
    )

    (
        configured_ranking_profiles,
        configured_ranking_feedback_repo,
        configured_ranker,
    ) = _build_ranking(
        embedding,
        settings,
        ranking_profiles,
        ranking_feedback_repo,
        ranker,
        cohere_client,
    )
    ranking_feedback = RankingFeedbackService(catalog, configured_ranking_feedback_repo)
    configured_calendar = _build_calendar(
        settings,
        calendar,
        google_calendar_access,
        google_calendar_bindings,
        google_calendar_client,
    )
    configured_object_store = (
        object_store if object_store is not None else _build_object_store(settings)
    )
    (
        configured_auth_context,
        configured_csrf_protection,
        configured_browser_session,
    ) = _build_identity_boundaries(
        settings,
        auth_context,
        csrf_protection,
        browser_session,
    )
    (
        configured_notifier,
        configured_notification_secret_protector,
        configured_vault,
        configured_action_audit,
        configured_registration_consent,
    ) = (
        notifier if notifier is not None else _build_notifier(settings),
        (
            notification_secret_protector
            if notification_secret_protector is not None
            else _build_notification_secret_protector(settings)
        ),
        (credential_vault if credential_vault is not None else _build_credential_vault(settings)),
        (action_audit if action_audit is not None else PostgresRegistrationActionAuditRepository()),
        (
            registration_consent
            if registration_consent is not None
            else PostgresRegistrationConsentEvidenceRepository()
        ),
    )
    configured_register_sources = _build_register_sources(settings, register_sources)
    configured_withdrawal_sources = _build_withdrawal_sources(settings, withdrawal_sources)
    (
        source_policies,
        configured_policy,
        configured_discovery_policy_gate,
        configured_source_quarantine,
    ) = _build_policy_graph(
        settings,
        policy_engine,
        discovery_policy_gate,
        source_quarantine,
    )
    _bind_register_source_policy_guards(configured_register_sources, configured_policy)
    configured_pacer, configured_browser_admission = (
        pacer or _build_pacer(settings),
        browser_admission or _build_browser_admission(settings),
    )
    budget_ledger, ticketmaster_budget = _build_ticketmaster_budget(
        configured_pacer, settings.ticketmaster_quota_scope
    )

    configured_public_crawler, catalog_fetchers, catalog_paged_fetchers = build_catalog_fetchers(
        settings
    )
    if discovery_sources is None:
        discovery_sources = (
            [configured_public_crawler] if configured_public_crawler is not None else []
        )

    parser = HeuristicRequestParser(embedding)
    discovery = DiscoveryService(
        discovery_sources,
        catalog,
        configured_discovery_policy_gate,
        configured_source_quarantine,
    )
    catalog_refresh = CatalogRefreshService(
        catalog_source_repo,
        catalog_refresh_committer,
        catalog_fetchers,
        configured_pacer,
        configured_discovery_policy_gate,
        configured_source_quarantine,
        catalog_source_repo,
        collection_windows=catalog_source_repo,
        lease_seconds=settings.catalog_refresh_lease_seconds,
    )
    catalog_paged_refresh = PagedCatalogRefreshService(
        catalog_source_repo,
        catalog_source_repo,
        catalog_paged_promoter,
        catalog_paged_fetchers,
        configured_pacer,
        configured_discovery_policy_gate,
        configured_source_quarantine,
        catalog_source_repo,
        collection_windows=catalog_source_repo,
        lease_seconds=settings.catalog_refresh_lease_seconds,
    )
    feed = FeedService(
        catalog,
        configured_ranker,
        configured_calendar,
        source_policies,
        membership_resolver=membership_resolver,
    )
    registration = RegistrationService(
        configured_register_sources,
        configured_policy,
        configured_pacer,
        configured_calendar,
        lifecycle_repo,
        handoff_repo,
        action_audit=configured_action_audit,
        registration_consent=configured_registration_consent,
        credential_vault=configured_vault,
        browser_admission=configured_browser_admission,
        source_quarantine=configured_source_quarantine,
        handoff_ttl_days=settings.handoff_ttl_days,
        handoff_completion_base_url=settings.public_base_url,
        require_https_completion_links=not settings.mock_cloud,
        notification_secret_protector=configured_notification_secret_protector,
        tenant_effect_authority=tenant_effect_authority,
        tenant_effect_timeout_seconds=settings.tenant_effect_timeout_seconds,
    )
    request_terminal = RequestTerminalService(request_repo)
    return Container(
        settings=settings,
        embedding=embedding,
        catalog=catalog,
        catalog_entities=catalog_entities,
        entity_intelligence=entity_intelligence,
        catalog_observation_repo=catalog_observation_repo,
        catalog_source_repo=catalog_source_repo,
        ingestion_admin_repo=ingestion_admin_repo,
        budget_ledger=budget_ledger,
        action_audit=configured_action_audit,
        account_erasure_repo=PostgresAccountErasureRepository(),
        tenant_repo=tenant_repo,
        consumer_accounts=PostgresConsumerAccountRepository(),
        consumer=consumer,
        request_repo=request_repo,
        lifecycle_repo=lifecycle_repo,
        handoff_repo=handoff_repo,
        handoff_expiry_repo=handoff_expiry_repo,
        lifecycle_invariant_repo=lifecycle_invariant_repo,
        outbox_repo=outbox_repo,
        outbox_wakeup=outbox_wakeup or PostgresOutboxWakeup(settings.database_url),
        change_detection_repo=change_detection_repo,
        watch_projection_outbox=watch_projection_outbox,
        calendar_repair_repo=calendar_repair_repo,
        ranking_profiles=configured_ranking_profiles,
        tenant_profiles=tenant_profiles or PostgresTenantProfileRepository(),
        profile_avatars=profile_avatars or PostgresProfileAvatarRepository(),
        media_store=media_store or _build_media_store(settings),
        profile_media_mutations=profile_media_mutations
        or PostgresProfileMediaMutationGuard(
            pool_capacity=settings.database_pool_size + settings.database_max_overflow,
            lock_timeout_seconds=settings.tenant_effect_lock_timeout_seconds,
            mutation_timeout_seconds=settings.tenant_effect_timeout_seconds,
        ),
        tenant_roles=tenant_roles or PostgresTenantRoleRepository(),
        api_keys=api_keys or PostgresApiKeyRepository(),
        muse=MuseSignupService(PostgresMuseRepository(), catalog),
        saved_catalog_filters=(saved_catalog_filters or PostgresSavedCatalogFilterRepository()),
        ranking_feedback_repo=configured_ranking_feedback_repo,
        ranking_feedback=ranking_feedback,
        ranker=configured_ranker,
        calendar=configured_calendar,
        notifier=configured_notifier,
        notification_secret_protector=configured_notification_secret_protector,
        vault=configured_vault,
        registration_consent=configured_registration_consent,
        policy=configured_policy,
        source_quarantine=configured_source_quarantine,
        discovery_policy_gate=configured_discovery_policy_gate,
        pacer=configured_pacer,
        browser_admission=configured_browser_admission,
        object_store=configured_object_store,
        auth_context=configured_auth_context,
        csrf_protection=configured_csrf_protection,
        browser_session=configured_browser_session,
        parser=parser,
        discovery=discovery,
        catalog_refresh=catalog_refresh,
        catalog_paged_refresh=catalog_paged_refresh,
        ticketmaster_budget=ticketmaster_budget,
        feed=feed,
        registration=registration,
        reconciliation=_build_reconciliation(
            configured_calendar,
            lifecycle_repo,
            handoff_repo,
            configured_policy,
            configured_pacer,
            configured_withdrawal_sources,
            configured_vault,
            configured_browser_admission,
            configured_source_quarantine,
            tenant_effect_authority,
            settings.tenant_effect_timeout_seconds,
            settings.handoff_ttl_days,
        ),
        handoff_reminders=HandoffReminderService(handoff_repo),
        request_terminal=request_terminal,
        source_policies=source_policies,
        tenant_effect_authority=tenant_effect_authority,
    )


def _build_reconciliation(
    calendar: CalendarPort,
    lifecycle_repo: PostgresLifecycleRepository,
    handoff_repo: PostgresHandoffRepository,
    policy: PolicyEngine,
    pacer: Pacer,
    withdrawals: dict[Source, RegistrationWithdrawalPort],
    vault: CredentialVault,
    browser_admission: BrowserAdmissionPort,
    source_quarantine: SourceQuarantinePort,
    tenant_effect_authority: TenantEffectAuthority,
    tenant_effect_timeout_seconds: float,
    handoff_ttl_days: int,
) -> LifecycleReconciliationService:
    """Keep post-booking lifecycle wiring at the composition boundary (FR-8.7/8.8)."""
    return LifecycleReconciliationService(
        calendar,
        lifecycle_repo,
        handoff_repo,
        policy,
        pacer,
        withdrawals,
        credential_vault=vault,
        browser_admission=browser_admission,
        source_quarantine=source_quarantine,
        tenant_effect_authority=tenant_effect_authority,
        tenant_effect_timeout_seconds=tenant_effect_timeout_seconds,
        handoff_ttl_days=handoff_ttl_days,
    )


def _build_ranking(
    embedding: DeterministicEmbedding,
    settings: Settings,
    ranking_profiles: RankingProfileRepository | None,
    ranking_feedback_repo: RankingFeedbackRepository | None,
    ranker: RankerPort | None,
    cohere_client: httpx.AsyncClient | None,
) -> tuple[RankingProfileRepository, RankingFeedbackRepository, RankerPort]:
    """Wire profiles and feedback, selecting Cohere only through an explicit secret setting."""
    configured_profiles = ranking_profiles or PostgresRankingProfileRepository()
    configured_feedback = ranking_feedback_repo or PostgresRankingFeedbackRepository()
    if ranker is not None:
        return configured_profiles, configured_feedback, ranker
    api_key = _cohere_api_key(settings)
    cross_encoder = (
        DeterministicCrossEncoder()
        if api_key is None
        else CohereRerankCrossEncoder(
            api_key,
            model=settings.cohere_rerank_model,
            client=cohere_client,
            timeout_s=settings.cohere_timeout_seconds,
        )
    )
    return (
        configured_profiles,
        configured_feedback,
        PersonalizedRanker(
            embedding=embedding,
            cross_encoder=cross_encoder,
            profiles=FeedbackAwareRankingProfiles(configured_profiles, configured_feedback),
            rescorer=DeterministicFeatureRescorer(),
        ),
    )


def _build_calendar(
    settings: Settings,
    calendar: CalendarPort | None,
    access: GoogleCalendarAccessPort | None,
    bindings: GoogleCalendarBindingPort | None,
    client: httpx.AsyncClient | None,
) -> CalendarPort:
    """Select Google only with an explicit flag and owner-provisioned tenant access boundary.

    A static environment bearer token would violate the per-tenant OAuth boundary. The still
    owner-gated access adapter is therefore required either as an injected port or a trusted local
    factory plugin, while the existing RLS binding repository supplies calendar targets by default
    (FR-1.3, FR-9.1/9.6).
    """
    # Direct root overrides are intentionally higher authority than settings so deployment tests
    # and a provisioned bootstrap can inject any port without changing global process settings.
    if calendar is not None:
        return calendar
    if not settings.google_calendar_enabled:
        if not settings.mock_cloud:
            raise ValueError(
                "non-mock deployments must inject a provisioned CalendarPort or enable "
                "Google Calendar with tenant-scoped access"
            )
        return MockCalendar()
    configured_access = access or _load_google_calendar_access(settings)
    if configured_access is None:
        raise ValueError(
            "Google Calendar enablement requires an injected GoogleCalendarAccessPort or "
            "google_calendar_access_factory"
        )
    return GoogleCalendarAdapter(
        configured_access,
        bindings or PostgresGoogleCalendarBindings(),
        client=client,
        timeout_s=settings.google_calendar_timeout_seconds,
    )


def _cohere_api_key(settings: Settings) -> str | None:
    """Reveal an explicitly configured model key only to the concrete composition root."""
    if settings.cohere_api_key is None:
        return None
    api_key = settings.cohere_api_key.get_secret_value()
    if not api_key.strip():
        raise ValueError("Cohere API key must not be empty when configured")
    return api_key.strip()


def _load_google_calendar_access(settings: Settings) -> GoogleCalendarAccessPort | None:
    """Load one owner-provided tenant-access adapter from an explicit deployment plugin path.

    The value names local deployment code, not a secret. It lets the ordinary API and Temporal
    worker entrypoints select Google through settings while keeping refresh-token/OAuth lifecycle
    outside this repository and rejecting a malformed bootstrap before any Calendar I/O.
    """
    configured = settings.google_calendar_access_factory
    if configured is None:
        return None
    module_name, separator, attribute_name = configured.strip().partition(":")
    if not separator or not module_name or not attribute_name or ":" in attribute_name:
        raise ValueError("google_calendar_access_factory must use the form 'module:callable'")
    try:
        module = import_module(module_name)
        factory = cast("Callable[[], object]", getattr(module, attribute_name))
    except (AttributeError, ImportError) as error:
        raise ValueError(
            "google_calendar_access_factory could not load its configured callable"
        ) from error
    if not callable(factory):
        raise ValueError("google_calendar_access_factory must resolve to a callable")
    access = factory()
    if not callable(getattr(access, "get_access", None)):
        raise ValueError("google_calendar_access_factory must return a GoogleCalendarAccessPort")
    return cast("GoogleCalendarAccessPort", access)


def _build_change_delivery_repositories() -> tuple[
    PostgresChangeDetectionRepository,
    PostgresLifecycleWatchProjectionOutbox,
    PostgresClosedWorkflowCalendarRepairRepository,
]:
    """Construct ADR-008's independent global control-plane repositories at the composition root."""
    return (
        PostgresChangeDetectionRepository(),
        PostgresLifecycleWatchProjectionOutbox(),
        PostgresClosedWorkflowCalendarRepairRepository(),
    )


def _build_pacer(settings: Settings) -> Pacer:
    """Select ADR-005 state explicitly, gating the unmeasured Meetup app contingency.

    The normal per-token shape can use the offline in-memory double.  A future per-app quota
    result needs shared Redis fairness and is deliberately unavailable unless the owner-run G2
    evidence flag is set; this does not enable a live Meetup adapter or declare an SLA.
    """
    meetup_app_mode = settings.meetup_quota_scope_mode == "per_app"
    if meetup_app_mode and not settings.meetup_app_g2_validated:
        raise ValueError(
            "Meetup per-app pacing requires owner-validated G2 quota-scope and cost evidence"
        )
    if settings.pacer_backend == "memory" and not settings.mock_cloud:
        raise ValueError("non-mock deployments must use the shared Redis Pacer")
    if _uses_shared_redis(settings):
        return RedisPacer(
            settings.redis_url,
            rate_per_sec=settings.pacer_rate_per_second,
            burst=settings.pacer_burst,
            unavailable_retry_seconds=settings.pacer_unavailable_retry_seconds,
            state_retention_seconds=settings.pacer_state_retention_seconds,
            source_budgets=default_source_budgets(),
            meetup_app_quota_scope=(settings.meetup_app_quota_scope if meetup_app_mode else None),
            meetup_app_degrade_after_seconds=settings.meetup_app_degrade_after_seconds,
        )
    return InMemoryPacer(
        rate_per_sec=settings.pacer_rate_per_second,
        burst=settings.pacer_burst,
        source_budgets=default_source_budgets(),
    )


def _build_browser_admission(settings: Settings) -> BrowserAdmissionPort:
    """Build the AC-45 browser capacity boundary beside, not inside, source-rate pacing.

    The mock graph gets a deterministic in-process double.  Any shared-pacing deployment uses
    Redis so all activity workers see the same 135-slot cap and recovery fence (NFR-4b, ADR-005).
    """
    if _uses_shared_redis(settings):
        return RedisBrowserAdmission(
            settings.redis_url,
            capacity=settings.browser_pool_capacity,
            lease_seconds=settings.browser_admission_lease_seconds,
            unavailable_retry_seconds=settings.pacer_unavailable_retry_seconds,
        )
    return InMemoryBrowserAdmission(
        capacity=settings.browser_pool_capacity,
        lease_seconds=settings.browser_admission_lease_seconds,
    )


def _build_media_store(settings: Settings) -> MediaStorePort:
    """API and erasure workers resolve the same explicitly selected durable media backend."""
    if settings.media_backend == "gcs":
        return build_gcs_media_store(settings)
    return LocalFilesystemMediaStore(Path(settings.media_local_root))


def _build_object_store(settings: Settings) -> ObjectStorePort:
    """Keep the local claim-check mock shared across processes; production must inject its store.

    A concrete cloud object store needs its own credential/KMS activation rather than an implicit
    filesystem fallback.  The injected port keeps that owner-gated integration outside the offline
    foundation (FR-8.5, FR-10.5, ADR-003/010).
    """
    if not settings.mock_cloud:
        raise ValueError("non-mock deployments must inject a provisioned ObjectStorePort")
    return MockFilesystemObjectStore(Path(settings.claim_check_local_root))


def _build_identity_boundaries(
    settings: Settings,
    auth_context: AuthContextPort | None,
    csrf_protection: CsrfProtectionPort | None,
    browser_session: BrowserSessionLifecyclePort | None,
) -> tuple[AuthContextPort, CsrfProtectionPort, BrowserSessionLifecyclePort | None]:
    """Select one coherent auth/CSRF/session graph and reject mixed authorities.

    A lifecycle adapter is necessarily both the authentication and CSRF authority for its opaque
    cookie. Allowing separately injected implementations beside it would let the tenant and CSRF
    checks resolve different sessions, so identity comparison is deliberately by object identity.
    """
    if browser_session is not None:
        if auth_context is not None and auth_context is not browser_session:
            raise ValueError("browser session must be the configured AuthContextPort")
        if csrf_protection is not None and csrf_protection is not browser_session:
            raise ValueError("browser session must be the configured CsrfProtectionPort")
        return browser_session, browser_session, browser_session

    if settings.identity_platform_enabled:
        if auth_context is not None or csrf_protection is not None:
            raise ValueError("Identity Platform cannot be combined with injected auth boundaries")
        assert settings.identity_platform_project_id
        configured_identity = IdentityPlatformBrowserSessionAdapter(
            verifier=IdentityPlatformVerifier(
                settings.identity_platform_project_id,
                settings.identity_platform_providers,
                settings.identity_platform_google_client_id,
            ),
            tenant_lookup=PostgresTenantRepository().get,
            trusted_origin=settings.public_base_url.rstrip("/"),
            redis_url=settings.redis_url,
            login_ttl_seconds=settings.oidc_login_ttl_seconds,
            session_ttl_seconds=settings.oidc_session_ttl_seconds,
            store_timeout_seconds=settings.oidc_session_store_timeout_seconds,
        )
        return configured_identity, configured_identity, configured_identity

    if settings.oidc_bff_enabled:
        if auth_context is not None or csrf_protection is not None:
            raise ValueError("built-in OIDC BFF cannot be combined with injected auth boundaries")
        configured = _build_oidc_bff_session(settings)
        return configured, configured, configured

    return (
        auth_context if auth_context is not None else _build_auth_context(settings),
        csrf_protection if csrf_protection is not None else _build_csrf_protection(settings),
        None,
    )


def _build_oidc_bff_session(settings: Settings) -> OidcBffSessionAdapter:
    """Build the repository-owned production BFF only from a complete validated settings set."""
    values = (
        settings.oidc_issuer,
        settings.oidc_authorization_url,
        settings.oidc_token_url,
        settings.oidc_jwks_url,
        settings.oidc_client_id,
    )
    if any(value is None for value in values) or settings.oidc_client_secret is None:
        # Settings normally rejects this first; keep direct composition calls fail closed too.
        raise ValueError("built-in OIDC BFF requires complete provider configuration")
    issuer, authorization_url, token_url, jwks_url, client_id = cast(
        "tuple[str, str, str, str, str]", values
    )
    public_origin = settings.public_base_url.rstrip("/")
    return OidcBffSessionAdapter(
        issuer=issuer,
        authorization_url=authorization_url,
        token_url=token_url,
        jwks_url=jwks_url,
        client_id=client_id,
        client_secret=settings.oidc_client_secret.get_secret_value().strip(),
        tenant_claim=settings.oidc_tenant_claim,
        provider=settings.oidc_provider,
        google_tenant_lookup=(
            PostgresTenantRepository().resolve_google_tenant
            if settings.oidc_provider == "google"
            else None
        ),
        google_identity_ready=(
            PostgresTenantRepository().google_identity_is_ready
            if settings.oidc_provider == "google"
            else None
        ),
        redirect_uri=f"{public_origin}/auth/callback",
        trusted_origin=public_origin,
        redis_url=settings.redis_url,
        algorithms=settings.oidc_algorithm_allowlist,
        login_ttl_seconds=settings.oidc_login_ttl_seconds,
        session_ttl_seconds=settings.oidc_session_ttl_seconds,
        http_timeout_seconds=settings.oidc_http_timeout_seconds,
        store_timeout_seconds=settings.oidc_session_store_timeout_seconds,
    )


def _build_auth_context(settings: Settings) -> AuthContextPort:
    """Select the local header seam only for mock deployments (FR-1.1, AC-1/AC-2).

    A real deployment must inject its OIDC/BFF adapter explicitly.  Falling back to a caller-set
    header outside ``mock_cloud`` would turn a test convenience into a tenant-impersonation path.
    """
    if not settings.mock_cloud:
        raise ValueError("non-mock deployments must inject a provisioned AuthContextPort")
    return HeaderAuthContext()


def _build_csrf_protection(settings: Settings) -> CsrfProtectionPort:
    """Select a no-op only when authentication itself is the explicit local request header.

    Production cookie/session semantics are deployment-owned, so non-mock composition must inject
    the matching verifier instead of guessing token names, session formats, or origin policy here.
    """
    if not settings.mock_cloud:
        raise ValueError("non-mock deployments must inject a provisioned CsrfProtectionPort")
    return LocalHeaderCsrfProtection()


def _build_notifier(settings: Settings) -> NotificationPort:
    """Keep the process-local delivery recorder strictly inside mock composition."""
    if not settings.mock_cloud:
        raise ValueError("non-mock deployments must inject a provisioned NotificationPort")
    return MockNotifier()


def _build_notification_secret_protector(settings: Settings) -> NotificationSecretProtector:
    """Use a stable local-only AEAD key; production must provision KMS/envelope protection."""
    if not settings.mock_cloud:
        raise ValueError(
            "non-mock deployments must inject a provisioned NotificationSecretProtector"
        )
    return DevelopmentNotificationSecretProtector()


def _build_credential_vault(settings: Settings) -> CredentialVault:
    """Never retain production credentials in the process-local mock vault."""
    if not settings.mock_cloud:
        raise ValueError("non-mock deployments must inject a provisioned CredentialVault")
    return MockVault()


def _build_register_sources(
    settings: Settings, sources: dict[Source, SourcePort] | None
) -> dict[Source, SourcePort]:
    """Require production registration enablement or disablement to be an explicit decision."""
    if sources is None:
        if not settings.mock_cloud:
            raise ValueError("non-mock deployments must inject an explicit register source map")
        return {}
    return sources


def _build_withdrawal_sources(
    settings: Settings,
    sources: dict[Source, RegistrationWithdrawalPort] | None,
) -> dict[Source, RegistrationWithdrawalPort]:
    """Require production withdrawal enablement or disablement to be explicit."""
    if sources is None:
        if not settings.mock_cloud:
            raise ValueError("non-mock deployments must inject an explicit withdrawal source map")
        return {}
    return sources


def _uses_shared_redis(settings: Settings) -> bool:
    """Keep all global P1d/P1e control planes off process-local state (ADR-005)."""
    return settings.uses_shared_pacer_redis


def _build_ticketmaster_budget(
    pacer: Pacer, quota_scope: str
) -> tuple[PostgresBudgetLedger, TicketmasterDispatchGate]:
    """Wire the ADR-002 ledger ahead of the future Ticketmaster adapter only at composition."""
    ledger = PostgresBudgetLedger()
    return ledger, TicketmasterDispatchGate(ledger, pacer, quota_scope)


def _build_policy(
    settings: Settings, override: PolicyEngine | None = None
) -> tuple[dict[Source, SourcePolicy], PolicyEngine]:
    """Keep feed routing defaults separate from the durable action-boundary PDP.

    Feed lane plans are advisory and static for the current request; the actual policy decision is
    re-read from PostgreSQL immediately before every source mutation, so a quarantine or kill
    switch cannot be bypassed by a stale feed (FR-5.9, FR-7.2, ADR-004).
    """
    source_policies = default_source_policies()
    return (
        (
            source_policies,
            StoreBackedPolicyEngine(
                PostgresPolicySnapshotRepository(),
                forced_kill_switch=settings.kill_switch,
            ),
        )
        if override is None
        else (source_policies, override)
    )


def _build_policy_graph(
    settings: Settings,
    policy_override: PolicyEngine | None,
    discovery_policy_override: DiscoveryPolicyGate | None,
    source_quarantine_override: SourceQuarantinePort | None,
) -> tuple[
    dict[Source, SourcePolicy],
    PolicyEngine,
    DiscoveryPolicyGate,
    SourceQuarantinePort,
]:
    """Assemble independent mutation and discovery policy boundaries at the composition root."""
    source_policies, policy = _build_policy(settings, policy_override)
    return (
        source_policies,
        policy,
        discovery_policy_override
        or StoreBackedDiscoveryPolicyGate(PostgresDiscoveryPolicyReader()),
        source_quarantine_override or PostgresSourceQuarantineRepository(),
    )


def _bind_register_source_policy_guards(
    sources: dict[Source, SourcePort] | None,
    policy: PolicyEngine,
) -> None:
    """Bind source-local policy guards that need to fence internal browser submits.

    ``LumaSource`` performs a read-only detect inside its SourcePort ``register`` call before it
    can submit. The application guard therefore fences the call entry, while this composition-only
    binding fences the adapter's detect-to-submit interval with the exact same PDP (ADR-004).
    """
    if sources is None:
        return
    for source in sources.values():
        if isinstance(source, LumaSource):
            source.bind_policy(policy)


def build_catalog_fetchers(
    settings: Settings,
) -> tuple[
    SourcePort | None,
    dict[CatalogSourceMode, CatalogSourceFetcher],
    dict[CatalogSourceMode, CatalogPagedSourceFetcher],
]:
    """Share the reviewed adapter registry between product and catalog-only composition."""
    catalog_fetchers: dict[CatalogSourceMode, CatalogSourceFetcher] = {}
    catalog_paged_fetchers: dict[CatalogSourceMode, CatalogPagedSourceFetcher] = {}
    configured_public_crawler: SourcePort | None = None
    if Source.PUBLIC_JSONLD.value in settings.enabled_sources:
        from .adapters.bibliocommons.source import BiblioCommonsCatalogFetcher
        from .adapters.civic_engage.source import CivicEngageRssCatalogFetcher
        from .adapters.communico.source import CommunicoCatalogFetcher
        from .adapters.crawl.source import PublicJsonLdSource
        from .adapters.datasf.source import DataSfOur415CatalogFetcher
        from .adapters.legistar.source import LegistarCatalogFetcher
        from .adapters.libcal.source import LibCalIcsCatalogFetcher
        from .adapters.livewhale.source import LiveWhaleCatalogFetcher
        from .adapters.localist.source import LocalistCatalogFetcher
        from .adapters.luma_calendar.source import LumaCalendarCatalogFetcher
        from .adapters.luma_discover.source import LumaDiscoverCatalogFetcher
        from .adapters.meetup_city.source import MeetupCityCatalogFetcher
        from .adapters.meetup_group.source import MeetupGroupCalendarCatalogFetcher
        from .adapters.midpen.source import MidpenCatalogFetcher
        from .adapters.oakland.source import OaklandCatalogFetcher
        from .adapters.sf_gov.source import SfGovCatalogFetcher
        from .adapters.tech_week.source import TechWeekCatalogFetcher
        from .adapters.tribe.source import TribeEventsCatalogFetcher

        configured_public_crawler = PublicJsonLdSource(
            user_agent=settings.crawl_user_agent,
            min_interval_ms=settings.crawl_min_interval_ms,
            seed_urls=settings.crawl_seeds,
        )
        catalog_fetchers[CatalogSourceMode.PUBLIC_JSONLD] = configured_public_crawler
        catalog_fetchers[CatalogSourceMode.LIVEWHALE_JSON] = LiveWhaleCatalogFetcher(
            user_agent=settings.crawl_user_agent,
        )
        catalog_fetchers[CatalogSourceMode.SF_GOV_JSON] = SfGovCatalogFetcher(
            user_agent=settings.crawl_user_agent,
        )
        catalog_fetchers[CatalogSourceMode.DATASF_OUR415] = DataSfOur415CatalogFetcher(
            user_agent=settings.crawl_user_agent,
        )
        catalog_fetchers[CatalogSourceMode.BIBLIOCOMMONS_RSS] = BiblioCommonsCatalogFetcher(
            user_agent=settings.crawl_user_agent,
        )
        catalog_fetchers.update(
            {
                CatalogSourceMode.COMMUNICO_JSON: CommunicoCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.LOCALIST_JSON: LocalistCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.LIBCAL_ICS: LibCalIcsCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.CIVIC_ENGAGE_RSS: CivicEngageRssCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.MIDPEN_HTML: MidpenCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.USFCA_HTML: UsfcaCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.CAL_PERFORMANCES_JSON: CalPerformancesCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.BERKELEY_REP_HTML: BerkeleyRepCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.YBCA_HTML: YbcaCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.OAKLAND_HTML: OaklandCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.LUMA_CALENDAR_JSON: LumaCalendarCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.LUMA_DISCOVER_JSON: LumaDiscoverCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.MEETUP_CITY_JSONLD: MeetupCityCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.MEETUP_GROUP_ICS: MeetupGroupCalendarCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
                CatalogSourceMode.TECH_WEEK_MCP: TechWeekCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
            }
        )
        legistar_fetcher = LegistarCatalogFetcher(
            user_agent=settings.crawl_user_agent,
        )
        catalog_fetchers.update(
            {
                CatalogSourceMode.SAN_JOSE_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.SUNNYVALE_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.ALAMEDA_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.OAKLAND_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.TRIBE_EVENTS_JSON: TribeEventsCatalogFetcher(
                    user_agent=settings.crawl_user_agent,
                ),
            }
        )
        catalog_paged_fetchers.update(
            {
                CatalogSourceMode.SAN_JOSE_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.SUNNYVALE_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.ALAMEDA_LEGISTAR: legistar_fetcher,
                CatalogSourceMode.OAKLAND_LEGISTAR: legistar_fetcher,
            }
        )
    return configured_public_crawler, catalog_fetchers, catalog_paged_fetchers
