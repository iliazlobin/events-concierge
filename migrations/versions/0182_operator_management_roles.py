"""Separate operator reads, control writes, and catalog execution from consumers.

Revision ID: 0182
Revises: 0181

Capability roles start NOLOGIN; deployment provisions each process's login separately.
No role membership or operator capability is granted to ec_app. The fixed aggregate
projection reveals queue health without tenant identities, payloads, or raw failures.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0182"
down_revision: str | None = "0181"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEWER = "ec_operator_viewer"
_CONTROLLER = "ec_operator_controller"
_EXECUTOR = "ec_ingestion_executor"
_AGGREGATE_DEFINER = "ec_operator_aggregate_definer"
_AGGREGATE_FUNCTIONS = (
    "fn_get_operator_schema_head_v1",
    "fn_get_operator_backend_overview_v1",
)
_AGGREGATE_QUEUE_TABLES = (
    "request_start_outbox",
    "outbox",
    "account_erasure_requests",
    "event_change_deliveries",
    "event_change_calendar_repairs",
    "handoff_expiry_queue",
    "lifecycle_watch_projection_outbox",
)
_AGGREGATE_RLS_TABLES = ("account_erasure_requests",)
_AGGREGATE_CATALOG_TABLES = (
    "ingestion_admin_commands",
    "catalog_entities",
    "catalog_entity_external_sources",
    "alembic_version",
)

# Explicit inventories prevent a future similarly named mutation from inheriting authority.
_READ_FUNCTIONS = (
    "fn_get_ingestion_admin_overview_v2",
    "fn_get_ingestion_admin_fleet_summary_v1",
    "fn_get_ingestion_admin_stage_summary_v1",
    "fn_get_ingestion_fleet_shape_v1",
    "fn_get_ingestion_throughput_v1",
    "fn_list_ingestion_admin_source_health_v1",
    "fn_list_ingestion_admin_sources_v6",
    "fn_count_ingestion_admin_sources_v2",
    "fn_list_ingestion_admin_runs_v4",
    "fn_count_ingestion_admin_runs_v2",
    "fn_list_ingestion_admin_filter_values_v2",
    "fn_get_ingestion_admin_source_detail_v2",
    "fn_get_ingestion_admin_source_summary",
    "fn_list_ingestion_admin_source_history",
    "fn_list_ingestion_admin_commands_v3",
    "fn_get_ingestion_admin_command_v3",
    "fn_get_ingestion_admin_run",
    "fn_list_ingestion_admin_command_runs_v1",
    "fn_list_ingestion_admin_due_sources_v3",
    "fn_get_catalog_freshness_summary_v1",
    "fn_get_catalog_concentration_v1",
    "fn_report_catalog_source_coverage_v1",
    "fn_browse_filtered_current_catalog_events_v10",
    "fn_list_current_catalog_providers_v3",
    "fn_lifecycle_invariant_snapshot",
    "fn_get_operator_backend_overview_v1",
    "fn_get_operator_schema_head_v1",
)
_CONTROL_FUNCTIONS = (
    "fn_enqueue_ingestion_admin_command_v2",
    "fn_update_ingestion_admin_source_configuration_v2",
    "fn_set_ingestion_admin_sources_enabled_v2",
)
_EXECUTION_FUNCTIONS = (
    "fn_claim_ingestion_admin_commands_v2",
    "fn_renew_ingestion_admin_command_lease",
    "fn_link_ingestion_admin_command_run_v1",
    "fn_complete_ingestion_admin_command",
    "fn_defer_ingestion_admin_command",
    "fn_fail_ingestion_admin_command",
    "fn_list_ingestion_admin_due_sources_v3",
    "fn_get_ingestion_admin_command_v3",
    "fn_list_ingestion_admin_command_runs_v1",
    "fn_list_due_catalog_refreshes",
    "fn_claim_catalog_refresh",
    "fn_has_live_catalog_refresh_lease",
    "fn_complete_catalog_refresh",
    "fn_fail_catalog_refresh",
    "fn_get_catalog_refresh_run",
    "fn_record_catalog_refresh_run_execution_v1",
    "fn_record_catalog_refresh_run_stage_v1",
    "fn_prepare_paged_catalog_refresh",
    "fn_stage_paged_catalog_refresh_page_v4",
    "fn_pause_paged_catalog_refresh",
    "fn_abort_paged_catalog_refresh",
    "fn_promote_paged_catalog_refresh",
    "fn_read_paged_catalog_refresh_stage_v4",
    # Canonical and observation CHECK constraints invoke this immutable validator as caller.
    "fn_event_entity_profiles_valid",
    "fn_refresh_catalog_entity_index_v3",
    "fn_prune_catalog_entity_index_v1",
    "fn_record_catalog_entity_social_source_v1",
    "fn_quarantine_source",
    "fn_authorize_ticketmaster_dispatch",
    "fn_record_ticketmaster_dispatch_outcome",
    # Entity public-fact refresh uses the same executor, with no tenant-table authority.
    "fn_list_catalog_entities_due_for_refresh_v1",
    "fn_get_catalog_entity_v1",
    "fn_list_catalog_entity_events_v2",
    "fn_get_catalog_entity_insights_v2",
    "fn_list_catalog_entity_external_sources_v1",
    "fn_list_catalog_entity_external_facts_v1",
    "fn_replace_catalog_entity_external_source_v1",
)


def upgrade() -> None:
    bind = op.get_bind()
    for role in (_VIEWER, _CONTROLLER, _EXECUTOR):
        if not bind.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
            {"role": role},
        ).scalar_one():
            op.execute(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        if bind.execute(
            text(
                "SELECT rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole "
                "OR rolcreatedb OR rolreplication "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError(f"operator capability role {role} has unsafe cluster privileges")
        if bind.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members membership "
                "JOIN pg_roles member ON member.oid=membership.member "
                "JOIN pg_roles target ON target.oid=membership.roleid "
                "WHERE member.rolname=:role "
                "AND NOT (:role=:controller AND target.rolname=:viewer))"
            ),
            {"role": role, "controller": _CONTROLLER, "viewer": _VIEWER},
        ).scalar_one():
            raise RuntimeError(
                f"operator capability role {role} has unexpected inherited authority"
            )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        if bind.execute(
            text(
                "SELECT pg_has_role('ec_app', :role, 'MEMBER') "
                "OR pg_has_role(:role, 'ec_app', 'MEMBER')"
            ),
            {"role": role},
        ).scalar_one():
            raise RuntimeError("consumer and operator/executor roles must not inherit each other")
    op.execute(f"GRANT {_VIEWER} TO {_CONTROLLER}")

    # Close legacy entry points as well as the latest wrappers. SECURITY DEFINER wrappers
    # retain access to their internal implementations without granting callers that bypass.
    functions = bind.execute(
        text(
            "SELECT p.oid::regprocedure::text AS signature FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname LIKE '%ingestion%'"
        )
    ).scalars()
    for signature in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, ec_app")
    _prepare_aggregate_definer()
    _create_aggregate_projection()
    _grant_functions(_VIEWER, _READ_FUNCTIONS)
    _grant_functions(_CONTROLLER, _CONTROL_FUNCTIONS)
    _grant_functions(_EXECUTOR, _EXECUTION_FUNCTIONS)
    # Existing public-catalog adapters use explicit tenant-neutral relational writes. No
    # tenant table, registry mutation, source-policy mutation, or operator audit DML is added.
    for table in (
        "canonical_events",
        "canonical_alias",
        "event_source_links",
        "catalog_event_observations",
    ):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO {_EXECUTOR}")
    for table in (
        "catalog_sources",
        "source_policy",
        "policy_global_control",
        "provider_budget_daily",
    ):
        op.execute(f"GRANT SELECT ON public.{table} TO {_EXECUTOR}")
    _transfer_aggregate_ownership()


def _prepare_aggregate_definer() -> None:
    """Use explicit SELECT policies under FORCE RLS, without managed-service BYPASSRLS."""
    bind = op.get_bind()
    if not bind.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
        {"role": _AGGREGATE_DEFINER},
    ).scalar_one():
        op.execute(
            f"CREATE ROLE {_AGGREGATE_DEFINER} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
    if bind.execute(
        text(
            "SELECT rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole "
            "OR rolcreatedb OR rolreplication FROM pg_roles WHERE rolname = :role"
        ),
        {"role": _AGGREGATE_DEFINER},
    ).scalar_one():
        raise RuntimeError("operator aggregate definer must be a restricted NOLOGIN role")
    # PostgreSQL 16 grants CREATEROLE creators ADMIN-only membership via the bootstrap
    # superuser. Preserve that administrative metadata solely for the trusted migration
    # owner so future migrations can manage this role; never admit a runtime role member.
    if bind.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_auth_members membership "
            "JOIN pg_roles target ON target.oid = membership.roleid "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE member.rolname = :role "
            "OR (target.rolname = :role AND member.rolname <> current_user))"
        ),
        {"role": _AGGREGATE_DEFINER},
    ).scalar_one():
        raise RuntimeError("operator aggregate definer must not have runtime role memberships")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_AGGREGATE_DEFINER}")
    for table in (*_AGGREGATE_QUEUE_TABLES, *_AGGREGATE_CATALOG_TABLES):
        op.execute(f"GRANT SELECT ON public.{table} TO {_AGGREGATE_DEFINER}")
    for table in _AGGREGATE_RLS_TABLES:
        op.execute(
            f"CREATE POLICY operator_aggregate_read ON public.{table} "
            f"FOR SELECT TO {_AGGREGATE_DEFINER} USING (true)"
        )


def _transfer_aggregate_ownership() -> None:
    """Revoke effective membership; retain only PG16's migration-owner ADMIN metadata."""
    bind = op.get_bind()
    migration_owner = bind.dialect.identifier_preparer.quote(
        bind.execute(text("SELECT current_user")).scalar_one()
    )
    op.execute(f"GRANT {_AGGREGATE_DEFINER} TO {migration_owner}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_AGGREGATE_DEFINER}")
    for name in _AGGREGATE_FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{name}() OWNER TO {_AGGREGATE_DEFINER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_AGGREGATE_DEFINER}")
    op.execute(f"REVOKE {_AGGREGATE_DEFINER} FROM {migration_owner}")
    if bind.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_auth_members membership "
            "JOIN pg_roles target ON target.oid=membership.roleid "
            "JOIN pg_roles member ON member.oid=membership.member "
            "WHERE target.rolname=:role AND (member.rolname <> current_user "
            "OR NOT membership.admin_option OR membership.inherit_option OR membership.set_option))"
        ),
        {"role": _AGGREGATE_DEFINER},
    ).scalar_one():
        raise RuntimeError("operator aggregate definer retained effective or runtime membership")


def _grant_functions(role: str, names: tuple[str, ...]) -> None:
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT p.proname, p.oid::regprocedure::text AS signature "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = ANY(:names)"
            ),
            {"names": list(names)},
        )
        .mappings()
        .all()
    )
    missing = set(names) - {row["proname"] for row in rows}
    if missing:
        raise RuntimeError(f"operator capability inventory is missing functions: {sorted(missing)}")
    for row in rows:
        op.execute(f"GRANT EXECUTE ON FUNCTION {row['signature']} TO {role}")


def _create_aggregate_projection() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_get_operator_schema_head_v1()
        RETURNS TABLE(version_num text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$ SELECT version_num::text FROM public.alembic_version ORDER BY version_num $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_operator_backend_overview_v1()
        RETURNS TABLE (
            queue text, pending bigint, ready bigint, leased bigint, failed bigint,
            oldest_pending_at timestamptz, last_progress_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH facts AS (
                SELECT 'request_start'::text AS queue, started_at IS NULL AS pending,
                       next_attempt_at AS ready_at, lease_expires_at,
                       started_at IS NULL AND last_error IS NOT NULL AS failed,
                       created_at, started_at AS progress_at
                FROM public.request_start_outbox
                UNION ALL
                SELECT 'notifications', delivered_at IS NULL AND failed_at IS NULL,
                       next_attempt_at, lease_expires_at, failed_at IS NOT NULL,
                       created_at, delivered_at
                FROM public.outbox
                UNION ALL
                SELECT 'account_erasure', status = 'erasing', next_attempt_at, lease_expires_at,
                       status = 'erasing' AND last_failure_stage IS NOT NULL, requested_at,
                       greatest(completed_at, external_effects_drained_at, workflows_cancelled_at,
                                calendar_purged_at, browser_sessions_revoked_at,
                                credential_vault_purged_at, object_store_purged_at)
                FROM public.account_erasure_requests
                UNION ALL
                SELECT 'change_delivery', delivered_at IS NULL, next_attempt_at, lease_expires_at,
                       delivered_at IS NULL AND last_error IS NOT NULL, created_at, delivered_at
                FROM public.event_change_deliveries
                UNION ALL
                SELECT 'calendar_repair', repaired_at IS NULL, next_attempt_at, lease_expires_at,
                       repaired_at IS NULL AND last_error IS NOT NULL, created_at, repaired_at
                FROM public.event_change_calendar_repairs
                UNION ALL
                SELECT 'handoff_expiry', resolved_at IS NULL, next_attempt_at, lease_expires_at,
                       resolved_at IS NULL AND last_error IS NOT NULL, created_at, resolved_at
                FROM public.handoff_expiry_queue
                UNION ALL
                SELECT 'watch_projection', delivered_at IS NULL, next_attempt_at, lease_expires_at,
                       delivered_at IS NULL AND last_error IS NOT NULL, created_at, delivered_at
                FROM public.lifecycle_watch_projection_outbox
                UNION ALL
                SELECT 'ingestion_commands', status IN ('queued', 'running'), available_at,
                       lease_expires_at, status = 'failed', requested_at, completed_at
                FROM public.ingestion_admin_commands
                UNION ALL
                SELECT 'entity_refresh',
                       count(source.source_id) = 0 OR min(source.next_refresh_at) <= statement_timestamp(),
                       coalesce(min(source.next_refresh_at), entity.created_at), NULL::timestamptz,
                       coalesce(bool_or(source.status IN ('failed', 'blocked')), false),
                       coalesce(min(source.next_refresh_at), entity.created_at), max(source.updated_at)
                FROM public.catalog_entities entity
                LEFT JOIN public.catalog_entity_external_sources source USING (entity_id)
                WHERE entity.identity_status = 'profile_verified'
                  AND entity.canonical_profile_url IS NOT NULL
                GROUP BY entity.entity_id
            ), queues(queue) AS (
                VALUES ('request_start'), ('notifications'), ('account_erasure'),
                       ('change_delivery'), ('calendar_repair'), ('handoff_expiry'),
                       ('watch_projection'), ('ingestion_commands'), ('entity_refresh')
            )
            SELECT queues.queue,
                   count(*) FILTER (WHERE facts.pending),
                   count(*) FILTER (WHERE facts.pending AND facts.ready_at <= statement_timestamp()
                                    AND (facts.lease_expires_at IS NULL
                                         OR facts.lease_expires_at <= statement_timestamp())),
                   count(*) FILTER (WHERE facts.pending AND facts.lease_expires_at > statement_timestamp()),
                   count(*) FILTER (WHERE facts.failed),
                   min(facts.created_at) FILTER (WHERE facts.pending),
                   max(facts.progress_at)
            FROM queues LEFT JOIN facts USING (queue)
            GROUP BY queues.queue
            ORDER BY queues.queue
        $$
        """
    )
    for name in _AGGREGATE_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC, ec_app")


def downgrade() -> None:
    raise RuntimeError(
        "0182 is a forward-only authority split; rollback application code with the operator roles "
        "retained, or use a separately reviewed forward migration. Consumer grants are not restored."
    )
