"""Real PostgreSQL authority, OCC, receipt, and aggregate-privacy proofs.

Every test uses the isolated runner's owner connection, selects the role under test
transaction-locally, and rolls back all product/control data. No login grants or provider
operations are needed to prove the production capability boundaries.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from events_concierge import catalog_runtime
from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.application.catalog_refresh import CatalogRefreshResult
from events_concierge.application.catalog_refresh_router import CatalogRefreshRouter
from events_concierge.application.ingestion_admin import IngestionAdminService
from events_concierge.config import Settings
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, Source
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.ingestion_admin import IngestionCommandAction
from events_concierge.infra import db as database

pytestmark = pytest.mark.integration

_ROLES = frozenset(
    {"ec_app", "ec_operator_viewer", "ec_operator_controller", "ec_ingestion_executor"}
)
_CONFIG = """
SELECT * FROM public.fn_update_ingestion_admin_source_configuration_v2(
    :source_key, :revision, :seed_url, CAST(:origins AS text[]), :mode,
    :enabled, true, NULL, :cadence, :pacing, :page_limit, :actor
)
"""
_ENQUEUE = """
SELECT public.fn_enqueue_ingestion_admin_command_v2(
    :id, 'refresh_source', :source, :actor, 'operator-integration', NULL
)
"""


@asynccontextmanager
async def _owner_transaction(*, empty_queues: bool = False) -> AsyncIterator[AsyncConnection]:
    url = os.environ.get("EC_MIGRATION_URL")
    if not url:
        pytest.skip("EC_MIGRATION_URL not set; use the isolated integration runner")
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if empty_queues:
                    # Aggregate queue tests need a known inventory. Earlier integration cases
                    # commit their own work; transactional TRUNCATE hides it only for this test,
                    # and the unconditional rollback restores all records for later cases.
                    await connection.execute(
                        text("TRUNCATE public.request_start_outbox, public.outbox")
                    )
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _role(connection: AsyncConnection, role: str | None) -> None:
    if role is None:
        await connection.execute(text("RESET ROLE"))
    else:
        assert role in _ROLES
        await connection.execute(text(f"SET LOCAL ROLE {role}"))


async def _denied(connection: AsyncConnection, statement: str) -> None:
    async with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text(statement))
        await savepoint.rollback()


async def _source(connection: AsyncConnection) -> str:
    source_key = f"operator-live-{uuid4().hex[:12]}"
    await connection.execute(
        text(
            """
            INSERT INTO public.catalog_sources (
                source_key, display_name, publisher, seed_url, approved_origins,
                region, mode, handoff_only, enabled, reviewed_at, review_expires_at,
                refresh_interval_minutes, min_interval_ms, page_limit, source_revision
            ) VALUES (
                :key, 'Reviewed Operator Source', 'Reviewed Publisher',
                :url, ARRAY['https://events.example.com'], 'bay_area_9_county',
                'public_jsonld', true, true, clock_timestamp(), NULL, 60, 1500, 1, 1
            )
            """
        ),
        {"key": source_key, "url": f"https://events.example.com/{source_key}"},
    )
    await connection.execute(
        text(
            "UPDATE public.source_policy SET automation_allowed = "
            "jsonb_set(automation_allowed, '{browser}', 'true'::jsonb), quarantined = false "
            "WHERE source = 'public_jsonld'"
        )
    )
    return source_key


async def test_operator_roles_do_not_inherit_tenant_or_control_authority() -> None:
    async with _owner_transaction() as connection:
        for role in _ROLES:
            assert not (
                await connection.execute(
                    text("SELECT pg_has_role(:role, 'ec_operator_aggregate_definer', 'MEMBER')"),
                    {"role": role},
                )
            ).scalar_one()
        for role in ("ec_operator_viewer", "ec_operator_controller", "ec_ingestion_executor"):
            assert not (
                await connection.execute(
                    text("SELECT pg_has_role('ec_app', :role, 'MEMBER')"), {"role": role}
                )
            ).scalar_one()
            assert not (
                await connection.execute(
                    text("SELECT pg_has_role(:role, 'ec_app', 'MEMBER')"), {"role": role}
                )
            ).scalar_one()
            await _role(connection, role)
            await _denied(connection, "SELECT notify_email FROM public.tenants LIMIT 1")
            await _denied(
                connection, "UPDATE public.catalog_sources SET enabled = false WHERE false"
            )
            await _denied(
                connection, "UPDATE public.source_policy SET quarantined = false WHERE false"
            )
            await _role(connection, None)

        await _role(connection, "ec_app")
        await _denied(connection, "SELECT * FROM public.fn_get_operator_backend_overview_v1()")
        await _denied(connection, "SELECT * FROM public.fn_get_ingestion_admin_overview_v2()")
        for function in (
            "fn_enqueue_ingestion_admin_command",
            "fn_enqueue_ingestion_admin_command_v2",
        ):
            args = "NULL, NULL, NULL, NULL" + (", NULL, NULL" if function.endswith("_v2") else "")
            await _denied(connection, f"SELECT public.{function}({args})")
        for function in (
            "fn_update_ingestion_admin_source_configuration",
            "fn_update_ingestion_admin_source_configuration_v2",
        ):
            await _denied(
                connection, f"SELECT * FROM public.{function}({', '.join(['NULL'] * 12)})"
            )

        for role in ("ec_operator_viewer", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT public.fn_enqueue_ingestion_admin_command_v2(NULL, NULL, NULL, NULL, NULL, NULL)",
            )
            await _denied(
                connection,
                "SELECT * FROM public.fn_set_ingestion_admin_sources_enabled_v2(NULL, NULL, NULL)",
            )
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT * FROM public.fn_claim_ingestion_admin_commands_v2(1, 300, 'integration', NULL)",
            )


async def test_source_configuration_derives_meetup_mode_and_fences_stale_edits() -> None:
    async with _owner_transaction() as connection:
        source = (
            (
                await connection.execute(
                    text("SELECT * FROM public.catalog_sources WHERE source_key = 'meetup-sf'")
                )
            )
            .mappings()
            .one()
        )
        assert source["mode"] == "meetup_city_jsonld"
        actor = f"operator:{uuid4()}"
        parameters = {
            "source_key": source["source_key"],
            "revision": source["source_revision"],
            "seed_url": source["seed_url"],
            "origins": source["approved_origins"],
            "mode": None,
            "enabled": source["enabled"],
            "cadence": source["refresh_interval_minutes"],
            "pacing": source["min_interval_ms"],
            "page_limit": source["page_limit"],
            "actor": actor,
        }
        await _role(connection, "ec_operator_controller")
        updated = (await connection.execute(text(_CONFIG), parameters)).mappings().one()
        assert updated["outcome"] == "updated"
        assert updated["source_revision"] == source["source_revision"] + 1
        replay = (await connection.execute(text(_CONFIG), parameters)).mappings().one()
        assert replay["outcome"] == "conflict"
        parameters["revision"] = updated["source_revision"]
        parameters["mode"] = "public_jsonld"
        rejected = (await connection.execute(text(_CONFIG), parameters)).mappings().one()
        assert rejected["outcome"] == "invalid"
        parameters["mode"] = source["mode"]
        legacy = (await connection.execute(text(_CONFIG), parameters)).mappings().one()
        assert legacy["outcome"] == "updated"
        await _role(connection, None)
        stored = (
            await connection.execute(
                text(
                    "SELECT mode, source_revision FROM public.catalog_sources WHERE source_key = 'meetup-sf'"
                )
            )
        ).one()
        assert stored.mode == source["mode"]
        assert stored.source_revision == source["source_revision"] + 2
        audit = (
            await connection.execute(
                text(
                    "SELECT before_config->>'mode', after_config->>'mode' "
                    "FROM public.catalog_source_configuration_audit WHERE requested_by = :actor"
                ),
                {"actor": actor},
            )
        ).all()
        assert audit == [("meetup_city_jsonld", "meetup_city_jsonld")] * 2


async def test_controller_receipt_executor_lease_and_publication_are_distinct() -> None:
    async with _owner_transaction() as connection:
        source_key = await _source(connection)
        command_id = uuid4()
        parameters = {"id": command_id, "source": source_key, "actor": f"operator:{uuid4()}"}
        await _role(connection, "ec_operator_controller")
        assert (await connection.execute(text(_ENQUEUE), parameters)).scalar_one() == "enqueued"
        assert (await connection.execute(text(_ENQUEUE), parameters)).scalar_one() == "replayed"
        assert (
            await connection.execute(text(_ENQUEUE), {**parameters, "actor": "different-operator"})
        ).scalar_one() == "conflict"

        await _role(connection, "ec_ingestion_executor")
        claims = (
            (
                await connection.execute(
                    text(
                        "SELECT * FROM public.fn_claim_ingestion_admin_commands_v2(100, 300, 'executor', NULL)"
                    )
                )
            )
            .mappings()
            .all()
        )
        claim = next(item for item in claims if item["command_id"] == command_id)
        lease = {"id": command_id, "lease": claim["lease_token"]}
        renewal = text("SELECT public.fn_renew_ingestion_admin_command_lease(:id, :lease, 300)")
        assert not (await connection.execute(renewal, {**lease, "lease": uuid4()})).scalar_one()
        assert (await connection.execute(renewal, lease)).scalar_one()
        run_key = f"admin:{command_id}"
        link = text(
            "SELECT public.fn_link_ingestion_admin_command_run_v1(:id, :attempt, :lease, :source, :run, 0)"
        )
        link_parameters = {
            **lease,
            "attempt": claim["attempt_count"],
            "source": source_key,
            "run": run_key,
        }
        assert (await connection.execute(link, link_parameters)).scalar_one()
        assert (await connection.execute(link, link_parameters)).scalar_one()
        result = json.dumps(
            {
                "action": "refresh_source",
                "source_key": source_key,
                "run_key": run_key,
                "outcome": "queued",
                "candidate_count": 0,
                "canonical_count": 0,
            }
        )
        complete = text(
            "SELECT public.fn_complete_ingestion_admin_command(:id, :lease, CAST(:result AS jsonb))"
        )
        assert not (
            await connection.execute(complete, {**lease, "lease": uuid4(), "result": result})
        ).scalar_one()
        assert (await connection.execute(complete, {**lease, "result": result})).scalar_one()

        await _role(connection, "ec_operator_viewer")
        command = (
            (
                await connection.execute(
                    text("SELECT * FROM public.fn_get_ingestion_admin_command_v3(:id)"),
                    {"id": command_id},
                )
            )
            .mappings()
            .one()
        )
        assert command["status"] == "completed"
        assert command["result"]["outcome"] == "queued"
        runs = (
            (
                await connection.execute(
                    text("SELECT * FROM public.fn_list_ingestion_admin_command_runs_v1(:id)"),
                    {"id": command_id},
                )
            )
            .mappings()
            .all()
        )
        assert len(runs) == 1
        assert runs[0]["run_key"] == run_key
        assert runs[0]["status"] == "pending"
        assert runs[0]["completed_at"] is None


async def test_backend_overview_measures_cross_tenant_queue_health_without_payloads() -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        before = {
            row["queue"]: row
            for row in (
                await connection.execute(
                    text("SELECT * FROM public.fn_get_operator_backend_overview_v1()")
                )
            ).mappings()
        }
        assert set(before) == {
            "request_start",
            "notifications",
            "account_erasure",
            "change_delivery",
            "calendar_repair",
            "handoff_expiry",
            "watch_projection",
            "ingestion_commands",
            "entity_refresh",
        }
        await _role(connection, None)
        tenant_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO public.tenants(tenant_id, oidc_subject, notify_email, relay_inbox) "
                "VALUES (:tenant, :subject, :email, :relay)"
            ),
            {
                "tenant": tenant_id,
                "subject": f"operator-test:{tenant_id}",
                "email": f"private-{tenant_id}@example.test",
                "relay": f"{tenant_id}@relay.example.test",
            },
        )
        for index in range(2):
            request_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO public.event_requests(request_id,tenant_id,raw_text) "
                    "VALUES (:request,:tenant,'private planning prompt never shown to operator')"
                ),
                {"request": request_id, "tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO public.request_start_outbox(request_id,tenant_id,dedup_key,created_at,"
                    "lease_token,lease_expires_at,last_error) VALUES (:request,:tenant,:dedup,"
                    "clock_timestamp()-interval '2 hours',:lease,"
                    "CASE WHEN :leased THEN clock_timestamp()+interval '5 minutes' ELSE NULL END,"
                    "'private provider diagnostic')"
                ),
                {
                    "request": request_id,
                    "tenant": tenant_id,
                    "dedup": str(request_id),
                    "lease": "test-live-lease" if index else None,
                    "leased": bool(index),
                },
            )
        await _role(connection, "ec_operator_viewer")
        after = {
            row["queue"]: row
            for row in (
                await connection.execute(
                    text("SELECT * FROM public.fn_get_operator_backend_overview_v1()")
                )
            ).mappings()
        }
        assert after["request_start"]["pending"] == before["request_start"]["pending"] + 2
        assert after["request_start"]["ready"] == before["request_start"]["ready"] + 1
        assert after["request_start"]["leased"] == before["request_start"]["leased"] + 1
        assert after["request_start"]["failed"] == before["request_start"]["failed"] + 2
        assert after["request_start"]["oldest_pending_at"] is not None
        serialized = json.dumps({key: dict(value) for key, value in after.items()}, default=str)
        assert str(tenant_id) not in serialized
        assert "private" not in serialized
        assert "test-live-lease" not in serialized
        assert all(
            set(row)
            == {
                "queue",
                "pending",
                "ready",
                "leased",
                "failed",
                "oldest_pending_at",
                "last_progress_at",
            }
            for row in after.values()
        )
        schema_heads = (
            (
                await connection.execute(
                    text("SELECT * FROM public.fn_get_operator_schema_head_v1()")
                )
            )
            .scalars()
            .all()
        )
        assert len(schema_heads) == 1


async def test_aggregate_installation_and_force_rls_reads_need_no_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reinstall the actual aggregate SQL as a CREATEROLE table owner with no RLS bypass."""
    migration = import_module("migrations.versions.0182_operator_management_roles")
    suffix = uuid4().hex[:12]
    migration_role = f"ec_aggregate_migration_{suffix}"
    definer_role = f"ec_aggregate_definer_{suffix}"
    async with _owner_transaction() as connection:
        tenant_id, request_id = uuid4(), uuid4()
        await connection.execute(
            text(
                "INSERT INTO public.account_erasure_requests(tenant_id, request_id, "
                "workflow_target_count, calendar_target_count, calendar_binding_expected) "
                "VALUES (:tenant,:request,0,0,false)"
            ),
            {"request": request_id, "tenant": tenant_id},
        )
        expected_pending = (
            await connection.execute(
                text("SELECT count(*) FROM public.account_erasure_requests WHERE status='erasing'")
            )
        ).scalar_one()
        assert expected_pending > 0
        await connection.execute(
            text(
                f"CREATE ROLE {migration_role} NOLOGIN CREATEROLE NOSUPERUSER "
                "NOBYPASSRLS NOCREATEDB NOREPLICATION"
            )
        )
        await connection.execute(
            text(f"GRANT USAGE, CREATE ON SCHEMA public TO {migration_role} WITH GRANT OPTION")
        )
        for table in (*migration._AGGREGATE_QUEUE_TABLES, *migration._AGGREGATE_CATALOG_TABLES):
            await connection.execute(text(f"ALTER TABLE public.{table} OWNER TO {migration_role}"))
        for table in migration._AGGREGATE_RLS_TABLES:
            await connection.execute(text(f"DROP POLICY operator_aggregate_read ON public.{table}"))
        for name in migration._AGGREGATE_FUNCTIONS:
            await connection.execute(text(f"DROP FUNCTION public.{name}()"))

        await connection.execute(text(f"SET LOCAL ROLE {migration_role}"))
        assert not (
            await connection.execute(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).scalar_one()
        assert (
            await connection.execute(text("SELECT count(*) FROM public.account_erasure_requests"))
        ).scalar_one() == 0

        def install(sync_connection: Connection) -> None:
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(sync_connection))
            )
            monkeypatch.setattr(migration, "_AGGREGATE_DEFINER", definer_role)
            migration._prepare_aggregate_definer()
            migration._create_aggregate_projection()
            migration._grant_functions("ec_operator_viewer", migration._AGGREGATE_FUNCTIONS)
            migration._transfer_aggregate_ownership()

        await connection.run_sync(install)
        await _role(connection, "ec_operator_viewer")
        pending = (
            await connection.execute(
                text(
                    "SELECT pending FROM public.fn_get_operator_backend_overview_v1() "
                    "WHERE queue='account_erasure'"
                )
            )
        ).scalar_one()
        assert pending == expected_pending
        await _denied(connection, "SELECT tenant_id FROM public.account_erasure_requests")
        await _role(connection, None)
        definer = (
            await connection.execute(
                text(
                    "SELECT role.rolcanlogin, role.rolsuper, role.rolbypassrls, "
                    "role.rolcreaterole, role.rolcreatedb, "
                    "has_schema_privilege(role.oid, 'public', 'CREATE') AS schema_create "
                    "FROM pg_roles role WHERE role.rolname=:role"
                ),
                {"role": definer_role},
            )
        ).one()
        memberships = (
            await connection.execute(
                text(
                    "SELECT target.rolname, member.rolname, grantor.rolname, "
                    "membership.admin_option, membership.inherit_option, membership.set_option "
                    "FROM pg_auth_members membership "
                    "JOIN pg_roles target ON target.oid=membership.roleid "
                    "JOIN pg_roles member ON member.oid=membership.member "
                    "JOIN pg_roles grantor ON grantor.oid=membership.grantor "
                    "WHERE target.rolname=:role OR member.rolname=:role"
                ),
                {"role": definer_role},
            )
        ).all()
        assert not any(definer)
        # PostgreSQL16 preserves creator ADMIN metadata, with no SET or inherited authority.
        assert len(memberships) == 1
        assert memberships[0][0] == definer_role
        assert memberships[0][1] == migration_role
        assert tuple(memberships[0][3:]) == (True, False, False)
        for table in (*migration._AGGREGATE_QUEUE_TABLES, *migration._AGGREGATE_CATALOG_TABLES):
            assert not (
                await connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, :table, 'INSERT,UPDATE,DELETE,TRUNCATE')"
                    ),
                    {"role": definer_role, "table": f"public.{table}"},
                )
            ).scalar_one()
        assert (
            await connection.execute(
                text(
                    "SELECT owner.rolname FROM pg_proc function "
                    "JOIN pg_roles owner ON owner.oid=function.proowner "
                    "WHERE function.oid='public.fn_get_operator_backend_overview_v1()'::regprocedure"
                )
            )
        ).scalar_one() == definer_role
        assert (
            await connection.execute(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE oid='public.account_erasure_requests'::regclass"
                )
            )
        ).scalar_one()


async def test_catalog_only_composition_executes_a_queued_refresh_with_executor_grants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the real graph and publication adapters; only publisher HTTP is replaced."""
    async with _owner_transaction() as connection:
        source_key = await _source(connection)
        candidate = CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"operator-publication-{uuid4()}",
            title=f"Reviewed public event {uuid4()}",
            start_at=datetime.now(UTC) + timedelta(days=2),
            registration_url=f"https://events.example.com/{source_key}/event",
            description="Deterministic public fixture; no publisher was contacted.",
        )
        calls = 0

        class FixtureFetcher:
            async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
                nonlocal calls
                assert source.source_key == source_key
                calls += 1
                return [candidate]

        # Service transactions use savepoints on the isolated owner connection after SET ROLE.
        # This tests actual executor privileges without creating a login or committing fixtures.
        sessions = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(database, "_sessionmaker", sessions)
        monkeypatch.setattr(
            catalog_runtime, "init_engine", lambda *_args, **_kwargs: connection.engine
        )
        monkeypatch.setattr(
            catalog_runtime,
            "build_catalog_fetchers",
            lambda _settings: (None, {CatalogSourceMode.PUBLIC_JSONLD: FixtureFetcher()}, {}),
        )
        settings = Settings(
            mock_cloud=True,
            admin_ingestion_enabled=False,
            pacer_backend="memory",
            ingestion_executor_enabled=True,
            ingestion_executor_database_url="postgresql+psycopg://executor:unused@localhost/isolated",
            claim_check_local_root=str(tmp_path / "claim-check"),
        )
        container = catalog_runtime.build_catalog_container(settings)
        assert not hasattr(container, "calendar")
        assert not hasattr(container, "notifications")
        command_id = uuid4()
        await _role(connection, "ec_operator_controller")
        controller = PostgresIngestionAdminRepository()
        await controller.enqueue(
            command_id, IngestionCommandAction.REFRESH_SOURCE, source_key, "operator:integration"
        )

        await _role(connection, "ec_ingestion_executor")
        await catalog_runtime.verify_catalog_executor_database()
        failures: list[Exception] = []

        class DiagnosticRouter(CatalogRefreshRouter):
            async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
                try:
                    return await super().refresh(source_key, run_key)
                except Exception as error:
                    failures.append(error)
                    raise

        router = DiagnosticRouter(container.catalog_source_repo, container.catalog_refresh, None)
        executor = IngestionAdminService(container.ingestion_admin_repo, router)
        report = await executor.process_once(limit=1)
        assert not failures, repr(failures)
        assert report.completed == 1
        assert report.failed == 0
        assert calls == 1
        assert (await executor.process_once(limit=1)).claimed == 0
        assert calls == 1

        await _role(connection, "ec_operator_viewer")
        detail = await controller.get_command_detail(command_id)
        assert detail is not None
        assert detail.command.result is not None
        assert detail.command.result["outcome"] == "succeeded"
        assert len(detail.runs) == 1
        assert detail.runs[0].status == "succeeded"
        assert detail.runs[0].canonical_count == 1
        assert detail.runs[0].completed_at is not None
