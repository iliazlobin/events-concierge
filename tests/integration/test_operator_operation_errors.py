"""Isolated PostgreSQL proofs for bounded, redacted operator error investigations."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role

from events_concierge.adapters.postgres.operator_operations import (
    PostgresOperatorOperationsRepository,
)
from events_concierge.api.operator_operations import OperationErrorsOut

pytestmark = pytest.mark.integration
_SIGNATURE = "public.fn_get_operator_errors_v1(text,integer,integer,uuid)"
_QUERY = "SELECT public.fn_get_operator_errors_v1(:queue,:offset,:limit,:record_id)"


async def _request(
    connection: AsyncConnection,
    tenant: UUID,
    *,
    state: str,
    error: str | None,
) -> UUID:
    identifier = uuid4()
    await connection.execute(
        text("""
        INSERT INTO public.event_requests(request_id,tenant_id,raw_text)
        VALUES(:id,:tenant,'private request text that must never reach the operator')
    """),
        {"id": identifier, "tenant": tenant},
    )
    await connection.execute(
        text("""
        INSERT INTO public.request_start_outbox(request_id,tenant_id,dedup_key,created_at,
            attempt_count,next_attempt_at,lease_token,lease_expires_at,last_error,started_at)
        VALUES(:id,:tenant,:dedup,'2000-01-01Z',1,
            CASE WHEN :state='scheduled' THEN '2099-01-02Z'::timestamptz ELSE '2000-01-02Z'::timestamptz END,
            'private-lease-token',CASE WHEN :state='leased' THEN '2099-01-03Z'::timestamptz END,
            :error,CASE WHEN :state='started' THEN statement_timestamp() END)
    """),
        {
            "id": identifier,
            "tenant": tenant,
            "dedup": f"private-dedup-{identifier}",
            "state": state,
            "error": error,
        },
    )
    return identifier


async def _entity(connection: AsyncConnection, *, verified: bool = True) -> UUID:
    identifier = uuid4()
    await connection.execute(
        text("""
        INSERT INTO public.catalog_entities(entity_id,identity_key,identity_status,kind,
            display_name,normalized_name,canonical_profile_url,profile_key,first_seen_at,last_seen_at,created_at)
        VALUES(:id,:identity,CASE WHEN :verified THEN 'profile_verified' ELSE 'source_scoped' END,
            'organization','Public Example Organization','public example organization',
            CASE WHEN :verified THEN :url END,CASE WHEN :verified THEN :url END,
            '2000-01-01Z','2000-01-01Z','2000-01-01Z')
    """),
        {
            "id": identifier,
            "identity": str(identifier),
            "verified": verified,
            "url": f"https://example.test/{identifier}",
        },
    )
    return identifier


async def _entity_source(
    connection: AsyncConnection,
    entity: UUID,
    provider: str,
    *,
    state: str = "failed",
    error: str | None = "unavailable",
    due: bool = False,
) -> UUID:
    identifier = uuid4()
    await connection.execute(
        text("""
        INSERT INTO public.catalog_entity_external_sources(source_id,entity_id,provider_key,
            external_id,source_url,display_name,status,next_refresh_at,error_code,updated_at)
        VALUES(:id,:entity,:provider,'external-public-reference','https://example.test/profile',
            'Public profile source',:state,
            CASE WHEN :due THEN '2000-01-01Z'::timestamptz ELSE '2099-01-02Z'::timestamptz END,
            :error,'2000-01-02Z')
    """),
        {
            "id": identifier,
            "entity": entity,
            "provider": provider,
            "state": state,
            "error": error,
            "due": due,
        },
    )
    return identifier


async def test_request_diagnostics_paginate_exact_records_without_raw_payloads() -> None:
    async with _owner_transaction(empty_queues=True) as connection:
        tenant = uuid4()
        await connection.execute(
            text("""
            INSERT INTO public.tenants(tenant_id,oidc_subject,notify_email,relay_inbox)
            VALUES(:id,:subject,'private@example.test','private@relay.example.test')
        """),
            {"id": tenant, "subject": f"diagnostics:{tenant}"},
        )
        fixture = await _request(
            connection, tenant, state="scheduled", error="fresh request-start retry after reclaim"
        )
        ready = await _request(
            connection, tenant, state="ready", error="Bearer private-secret provider payload"
        )
        leased = await _request(
            connection,
            tenant,
            state="leased",
            error="fresh request-start retry after reclaim plus private-secret",
        )
        await _request(connection, tenant, state="started", error="private-secret terminal")
        await _request(connection, tenant, state="ready", error=None)
        repo = PostgresOperatorOperationsRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        first = await repo.errors(queue="request_start", limit=2)
        second = await repo.errors(queue="request_start", offset=2, limit=2)
        assert first.total == second.total == 3
        assert [item.record_id for item in (*first.items, *second.items)] == sorted(
            [fixture, ready, leased]
        )
        assert (await repo.errors(queue="request_start", offset=100)).items == ()
        exact = await repo.errors(queue="request_start", record_id=fixture)
        assert exact.total == 1 and len(exact.items) == 1
        item = exact.items[0]
        assert item.state == "scheduled" and item.error_code == "unclassified"
        assert item.error_summary == "Error recorded."
        assert item.next_attempt_at == datetime(2099, 1, 2, tzinfo=UTC)
        assert item.attempt_count == 1 and item.last_observed_at is None and not item.sources
        rows = {row.record_id: row for row in (*first.items, *second.items)}
        assert rows[ready].state == "ready" and rows[leased].state == "leased"
        assert rows[ready].error_code == rows[leased].error_code == "unclassified"
        assert (await repo.errors(queue="request_start", record_id=uuid4())).total == 0
        assert (await repo.errors(queue="request_start", record_id=fixture, offset=1)).total == 1
        encoded = json.dumps([asdict(first), asdict(second)], default=str)
        assert str(tenant) not in encoded
        assert "private" not in encoded and "Bearer" not in encoded
        assert "dedup" not in encoded and "lease_token" not in encoded and "raw_text" not in encoded
        assert OperationErrorsOut.model_validate(first).items[0].record_id in {
            fixture,
            ready,
            leased,
        }


async def test_entity_errors_count_profiles_and_show_all_failed_sources() -> None:
    async with _owner_transaction(empty_queues=True) as connection:
        due = await _entity(connection)
        first = await _entity_source(connection, due, "github_public")
        second = await _entity_source(
            connection, due, "official_website", state="blocked", error="network_policy"
        )
        # Eligibility/due state use all source evidence, not just the failed-source subset.
        await _entity_source(
            connection, due, "linkedin_profile", state="linked", error=None, due=True
        )
        scheduled = await _entity(connection)
        await _entity_source(connection, scheduled, "official_website", error="invalid_response")
        excluded = await _entity(connection, verified=False)
        await _entity_source(connection, excluded, "official_website")
        repo = PostgresOperatorOperationsRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        page = await repo.errors(queue="entity_refresh", limit=1)
        later = await repo.errors(queue="entity_refresh", limit=1, offset=1)
        assert page.total == later.total == 2
        assert {page.items[0].record_id, later.items[0].record_id} == {due, scheduled}
        exact = (await repo.errors(queue="entity_refresh", record_id=due)).items[0]
        assert exact.state == "due" and exact.attempt_count is None
        assert exact.label == "Public Example Organization" and exact.error_code == "source_errors"
        assert {source.source_id for source in exact.sources} == {first, second}
        assert {source.error_code for source in exact.sources} == {"unavailable", "network_policy"}
        assert all(
            source.observed_at == datetime(2000, 1, 2, tzinfo=UTC) for source in exact.sources
        )
        assert (await repo.errors(queue="entity_refresh", record_id=scheduled)).items[
            0
        ].state == "scheduled"
        assert (await repo.errors(queue="entity_refresh", record_id=excluded)).total == 0
        overview = await repo.overview()
        assert (
            next(queue.failed for queue in overview.queues if queue.queue == "entity_refresh")
            == page.total
        )
        OperationErrorsOut.model_validate(page)


async def test_error_projection_roles_are_narrow_and_owner_stays_restricted() -> None:
    async with _owner_transaction(empty_queues=True) as connection:
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, "SELECT public.fn_get_operator_errors_v1('request_start')")
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            await connection.execute(
                text("SELECT public.fn_get_operator_errors_v1('request_start')")
            )
            await _denied(
                connection, "SELECT last_error,tenant_id FROM public.request_start_outbox"
            )
            await _denied(connection, "SELECT raw_text FROM public.event_requests")
            await _denied(connection, "SELECT * FROM public.catalog_entity_external_sources")
            await _denied(
                connection, "UPDATE public.request_start_outbox SET last_error=NULL WHERE false"
            )
        await _role(connection, None)
        record = (
            await connection.execute(
                text("""
            SELECT owner.rolname,owner.rolcanlogin,owner.rolsuper,owner.rolbypassrls,
                   function.prosecdef,function.proconfig,
                   has_schema_privilege(owner.oid,'public','CREATE') AS schema_create
            FROM pg_proc function JOIN pg_roles owner ON owner.oid=function.proowner
            WHERE function.oid=CAST(:signature AS regprocedure)
        """),
                {"signature": _SIGNATURE},
            )
        ).one()
        assert record.rolname == "ec_operator_aggregate_definer" and record.prosecdef
        assert not any(
            (record.rolcanlogin, record.rolsuper, record.rolbypassrls, record.schema_create)
        )
        assert record.proconfig == ["search_path=pg_catalog, public"]
        assert not (
            await connection.execute(
                text("""
            SELECT EXISTS(SELECT 1 FROM pg_auth_members membership JOIN pg_roles role ON role.oid=membership.roleid
                WHERE role.rolname='ec_operator_aggregate_definer' AND (membership.inherit_option OR membership.set_option))
        """)
            )
        ).scalar_one()


async def test_error_projection_installation_needs_no_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module("migrations.versions.0185_operator_error_diagnostics")
    suffix = uuid4().hex[:12]
    owner, definer = f"error_migration_{suffix}", f"error_definer_{suffix}"
    async with _owner_transaction(empty_queues=True) as connection:
        await connection.execute(text(f"DROP FUNCTION {_SIGNATURE}"))
        await connection.execute(
            text(f"CREATE ROLE {owner} NOLOGIN NOINHERIT CREATEROLE NOSUPERUSER NOBYPASSRLS")
        )
        await connection.execute(
            text(f"GRANT USAGE,CREATE ON SCHEMA public TO {owner} WITH GRANT OPTION")
        )
        await connection.execute(text(f"SET LOCAL ROLE {owner}"))
        await connection.execute(
            text(f"CREATE ROLE {definer} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS")
        )
        await _role(connection, None)
        await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {definer}"))
        await connection.execute(
            text(
                "GRANT SELECT ON public.request_start_outbox,public.catalog_entities,"
                f"public.catalog_entity_external_sources TO {definer}"
            )
        )
        await connection.execute(text(f"SET LOCAL ROLE {owner}"))

        def install(sync_connection: Connection) -> None:
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(sync_connection))
            )
            monkeypatch.setattr(migration, "_DEFINER", definer)
            migration.upgrade()

        await connection.run_sync(install)
        await _role(connection, "ec_operator_viewer")
        result = (
            await connection.execute(
                text("SELECT public.fn_get_operator_errors_v1('request_start')")
            )
        ).scalar_one()
        assert result["items"] == []
        await _denied(connection, "SELECT last_error FROM public.request_start_outbox")
        await _role(connection, None)
        assert not (
            await connection.execute(
                text(
                    "SELECT has_schema_privilege(:definer,'public','CREATE') OR EXISTS("
                    "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
                    "WHERE r.rolname=:definer AND (m.inherit_option OR m.set_option))"
                ),
                {"definer": definer},
            )
        ).scalar_one()


@pytest.mark.parametrize(
    "queue,offset,limit",
    [
        ("notifications", 0, 10),
        (None, 0, 10),
        ("request_start", -1, 10),
        ("entity_refresh", 10001, 10),
        ("request_start", 0, 51),
        ("entity_refresh", 0, None),
    ],
)
async def test_error_projection_validates_bounds_inside_database(
    queue: str | None,
    offset: int,
    limit: int | None,
) -> None:
    async with _owner_transaction(empty_queues=True) as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="invalid operator error query"):
            await connection.execute(
                text(_QUERY), {"queue": queue, "offset": offset, "limit": limit, "record_id": None}
            )
