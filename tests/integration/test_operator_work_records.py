"""Isolated PostgreSQL proofs for scoped, secret-free pending work records."""

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
from tests.integration.test_operator_operation_errors import _request

from events_concierge.adapters.postgres.operator_operations import (
    PostgresOperatorOperationsRepository,
)
from events_concierge.api.operator_operations import OperationRecordsOut

pytestmark = pytest.mark.integration
_SIGNATURE = "public.fn_get_operator_records_v1(text,text,integer,integer,text)"
_QUERY = "SELECT public.fn_get_operator_records_v1(:queue,:scope,:offset,:limit,:record_id)"


async def _tenant(connection: AsyncConnection) -> UUID:
    identifier = uuid4()
    await connection.execute(
        text("""INSERT INTO public.tenants(tenant_id,oidc_subject,notify_email,relay_inbox)
        VALUES(:id,:subject,'private@example.test','private@relay.example.test')"""),
        {"id": identifier, "subject": f"work-records:{identifier}"},
    )
    return identifier


async def _notification(
    connection: AsyncConnection,
    tenant: UUID,
    *,
    state: str,
    error: str | None = None,
    identifier: int | None = None,
) -> str:
    result = await connection.execute(
        text("""INSERT INTO public.outbox(
        id,tenant_id,topic,payload,created_at,attempt_count,next_attempt_at,lease_token,
        lease_expires_at,last_error,failed_at,delivered_at)
        VALUES(COALESCE(:id,nextval('public.outbox_id_seq')),:tenant,'private-topic',
        '{"recipient":"private@example.test","message":"private payload","protected_completion_url":"private-secret"}'::jsonb,
        '2000-01-01Z',0,CASE WHEN :state='scheduled' OR :state='failed' THEN '2099-01-02Z'::timestamptz ELSE '2000-01-02Z'::timestamptz END,
        'private-lease-token',CASE WHEN :state='leased' OR :state='failed' THEN '2099-01-03Z'::timestamptz END,
        :error,CASE WHEN :state='failed' THEN '2000-01-03Z'::timestamptz END,
        CASE WHEN :state='delivered' THEN '2000-01-04Z'::timestamptz END) RETURNING id"""),
        {"id": identifier, "tenant": tenant, "state": state, "error": error},
    )
    return str(result.scalar_one())


async def test_pending_requests_include_clean_records_and_errors_are_only_a_pending_subset() -> (
    None
):
    async with _owner_transaction() as connection:
        tenant = await _tenant(connection)
        clean = await _request(connection, tenant, state="ready", error=None)
        retry = await _request(
            connection, tenant, state="scheduled", error="fresh request-start retry after reclaim"
        )
        leased = await _request(connection, tenant, state="leased", error="Bearer private-secret")
        started = await _request(connection, tenant, state="started", error="private-error")
        repository = PostgresOperatorOperationsRepository(_scope(connection, "ec_operator_viewer"))
        first = await repository.records(queue="request_start", limit=2)
        second = await repository.records(queue="request_start", offset=2, limit=2)
        assert first.total == second.total == 3
        assert [row.record_id for row in (*first.items, *second.items)] == sorted(
            map(str, [clean, retry, leased])
        )
        errors = await repository.records(queue="request_start", scope="errors")
        assert errors.total == 2 and str(clean) not in {row.record_id for row in errors.items}
        row = (await repository.records(queue="request_start", record_id=str(clean))).items[0]
        assert row.state == "ready" and row.error_code is row.error_summary is None
        assert row.attempt_kind == "failed_start_attempts" and row.failed_at is None
        scheduled = (await repository.records(queue="request_start", record_id=str(retry))).items[0]
        assert scheduled.next_attempt_at == datetime(2099, 1, 2, tzinfo=UTC)
        assert scheduled.error_code == "test_retry_fixture"
        assert (await repository.records(queue="request_start", record_id=str(started))).total == 0
        assert (await repository.records(queue="request_start", offset=100)).items == ()
        assert (
            await repository.records(queue="request_start", scope="errors", record_id=str(clean))
        ).total == 0
        assert str(tenant) not in json.dumps(asdict(first), default=str)
        assert "private" not in json.dumps(asdict(errors), default=str)
        OperationRecordsOut.model_validate(first)


async def test_notifications_separate_pending_from_failed_history_and_redact_every_private_field() -> (
    None
):
    async with _owner_transaction() as connection:
        tenant = await _tenant(connection)
        clean = await _notification(connection, tenant, state="ready")
        retry = await _notification(
            connection,
            tenant,
            state="scheduled",
            error="notification materialization or delivery failed",
        )
        big = await _notification(
            connection,
            tenant,
            state="leased",
            error="private raw recipient payload",
            identifier=9007199254740993,
        )
        failed = await _notification(
            connection,
            tenant,
            state="failed",
            error="outbox projection contains a forbidden plaintext completion URL",
        )
        unknown = await _notification(
            connection,
            tenant,
            state="failed",
            error="notification materialization or delivery failed plus private-secret",
        )
        no_reason = await _notification(connection, tenant, state="failed")
        delivered = await _notification(connection, tenant, state="delivered")
        repository = PostgresOperatorOperationsRepository(_scope(connection, "ec_operator_viewer"))
        first = await repository.records(queue="notifications", limit=2)
        second = await repository.records(queue="notifications", offset=2, limit=2)
        assert first.total == second.total == 3
        assert [row.record_id for row in (*first.items, *second.items)] == sorted(
            [clean, retry, big], key=int
        )
        assert first.items[0].error_code is None
        assert first.items[1].error_code == "delivery_failed"
        assert second.items[0].record_id == "9007199254740993" and second.items[0].state == "leased"
        assert second.items[0].error_code == "unclassified"
        exact = (await repository.records(queue="notifications", record_id=big)).items[0]
        assert exact.attempt_kind == "failed_delivery_attempts" and exact.attempt_count == 0
        history = await repository.records(queue="notifications", scope="failed")
        assert history.total == 3
        rows = {row.record_id: row for row in history.items}
        assert set(rows) == {failed, unknown, no_reason}
        assert rows[failed].error_code == "unsafe_projection"
        assert rows[unknown].error_code == "unclassified"
        assert rows[no_reason].error_summary == "No failure reason was recorded."
        assert all(
            row.state == "failed" and row.next_attempt_at is row.lease_expires_at is None
            for row in history.items
        )
        assert (
            rows[failed].failed_at
            == rows[failed].last_observed_at
            == datetime(2000, 1, 3, tzinfo=UTC)
        )
        assert (await repository.records(queue="notifications", record_id=failed)).total == 0
        assert (
            await repository.records(queue="notifications", scope="failed", record_id=clean)
        ).total == 0
        assert (await repository.records(queue="notifications", record_id=delivered)).total == 0
        payload = json.dumps([asdict(first), asdict(second), asdict(history)], default=str)
        for secret in (str(tenant), "private", "recipient", "payload", "topic", "lease_token"):
            assert secret not in payload
        overview = await repository.overview()
        queue = next(item for item in overview.queues if item.queue == "notifications")
        assert queue.pending == first.total and queue.failed == history.total
        OperationRecordsOut.model_validate(history)


async def test_work_record_projection_keeps_operator_roles_narrow() -> None:
    async with _owner_transaction() as connection:
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, "SELECT public.fn_get_operator_records_v1('notifications')")
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            await connection.execute(
                text("SELECT public.fn_get_operator_records_v1('notifications')")
            )
            await _denied(connection, "SELECT payload,tenant_id,last_error FROM public.outbox")
            await _denied(connection, "SELECT last_error FROM public.request_start_outbox")
            await _denied(connection, "UPDATE public.outbox SET last_error=NULL WHERE false")
        await _role(connection, None)
        row = (
            await connection.execute(
                text("""SELECT role.rolname,role.rolcanlogin,role.rolsuper,role.rolbypassrls,
            function.prosecdef,function.proconfig,has_schema_privilege(role.oid,'public','CREATE') AS can_create
            FROM pg_proc function JOIN pg_roles role ON role.oid=function.proowner
            WHERE function.oid=CAST(:signature AS regprocedure)"""),
                {"signature": _SIGNATURE},
            )
        ).one()
        assert row.rolname == "ec_operator_aggregate_definer" and row.prosecdef
        assert not any((row.rolcanlogin, row.rolsuper, row.rolbypassrls, row.can_create))
        assert row.proconfig == ["search_path=pg_catalog, public"]


async def test_signed_notification_references_round_trip_without_losing_bigint_precision() -> None:
    references = ["-9223372036854775808", "-9222999999738606380", "-1", "0", "9223372036854775807"]
    async with _owner_transaction() as connection:
        tenant = await _tenant(connection)
        for reference in references:
            assert (
                await _notification(connection, tenant, state="ready", identifier=int(reference))
                == reference
            )
        failed = await _notification(connection, tenant, state="failed", identifier=-2)
        repository = PostgresOperatorOperationsRepository(_scope(connection, "ec_operator_viewer"))
        pages = [
            await repository.records(queue="notifications", offset=offset, limit=2)
            for offset in (0, 2, 4)
        ]
        assert all(page.total == len(references) for page in pages)
        assert [row.record_id for page in pages for row in page.items] == references
        for reference in references:
            exact = await repository.records(queue="notifications", record_id=reference)
            assert exact.total == 1 and exact.items[0].record_id == reference
            assert OperationRecordsOut.model_validate(exact).items[0].record_id == reference
        history = await repository.records(queue="notifications", scope="failed", record_id=failed)
        assert history.total == 1 and history.items[0].record_id == "-2"
        assert (await repository.records(queue="notifications", record_id=failed)).total == 0


@pytest.mark.parametrize(
    "queue,scope,offset,limit,record_id",
    [
        ("request_start", "failed", 0, 10, None),
        ("notifications", "errors", 0, 10, None),
        ("entity_refresh", "pending", 0, 10, None),
        (None, "pending", 0, 10, None),
        ("request_start", None, 0, 10, None),
        ("request_start", "pending", -1, 10, None),
        ("notifications", "pending", 10001, 10, None),
        ("request_start", "pending", 0, 51, None),
        ("notifications", "pending", 0, 10, "9223372036854775808"),
        ("notifications", "pending", 0, 10, "-9223372036854775809"),
        ("notifications", "pending", 0, 10, "01"),
        ("notifications", "pending", 0, 10, "-01"),
        ("notifications", "pending", 0, 10, "-0"),
        ("notifications", "pending", 0, 10, "+1"),
        ("request_start", "pending", 0, 10, "42"),
    ],
)
async def test_work_record_database_validates_scopes_bounds_and_typed_references(
    queue: str | None,
    scope: str | None,
    offset: int,
    limit: int,
    record_id: str | None,
) -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="invalid operator record query"):
            await connection.execute(
                text(_QUERY),
                {
                    "queue": queue,
                    "scope": scope,
                    "offset": offset,
                    "limit": limit,
                    "record_id": record_id,
                },
            )


@pytest.mark.parametrize("signed_upgrade", [False, True])
async def test_work_record_migration_installs_without_superuser_and_revokes_temporary_authority(
    monkeypatch: pytest.MonkeyPatch,
    signed_upgrade: bool,
) -> None:
    migration = import_module("migrations.versions.0188_operator_work_records")
    suffix = uuid4().hex[:12]
    owner, definer = f"records_migration_{suffix}", f"records_definer_{suffix}"
    async with _owner_transaction() as connection:
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
            text(f"GRANT SELECT ON public.request_start_outbox,public.outbox TO {definer}")
        )
        await connection.execute(text(f"SET LOCAL ROLE {owner}"))

        def install(sync_connection: Connection) -> None:
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(sync_connection))
            )
            monkeypatch.setattr(migration, "_DEFINER", definer)
            migration.upgrade()
            if signed_upgrade:
                statement = text(
                    "SELECT proowner,proacl FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
                )
                before = sync_connection.execute(statement, {"signature": _SIGNATURE}).one()
                forward = import_module("migrations.versions.0189_operator_signed_work_references")
                monkeypatch.setattr(
                    forward, "op", Operations(MigrationContext.configure(sync_connection))
                )
                monkeypatch.setattr(forward, "_DEFINER", definer)
                forward.upgrade()
                assert sync_connection.execute(statement, {"signature": _SIGNATURE}).one() == before

        await connection.run_sync(install)
        await _role(connection, "ec_operator_viewer")
        assert (
            await connection.execute(
                text("SELECT public.fn_get_operator_records_v1('notifications')")
            )
        ).scalar_one()["items"] == []
        if signed_upgrade:
            assert (
                await connection.execute(
                    text(
                        "SELECT public.fn_get_operator_records_v1('notifications','pending',0,10,'-9223372036854775808')"
                    )
                )
            ).scalar_one()["items"] == []
        await _denied(connection, "SELECT payload FROM public.outbox")
        await _role(connection, None)
        assert not (
            await connection.execute(
                text("""SELECT has_schema_privilege(:definer,'public','CREATE') OR EXISTS(
            SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
            WHERE r.rolname=:definer AND (m.inherit_option OR m.set_option))"""),
                {"definer": definer},
            )
        ).scalar_one()


async def test_signed_reference_forward_and_backward_replacement_preserves_records_and_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module("migrations.versions.0189_operator_signed_work_references")
    async with _owner_transaction() as connection:
        tenant = await _tenant(connection)
        reference = await _notification(
            connection, tenant, state="ready", identifier=-9222999999738606380
        )
        metadata = text(
            "SELECT proowner,proacl FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
        )
        before = (await connection.execute(metadata, {"signature": _SIGNATURE})).one()

        def replace(sync_connection: Connection, *, forward: bool) -> None:
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(sync_connection))
            )
            if forward:
                migration.upgrade()
            else:
                migration.downgrade()

        await connection.run_sync(lambda sync: replace(sync, forward=False))
        assert (await connection.execute(metadata, {"signature": _SIGNATURE})).one() == before
        await _role(connection, "ec_operator_viewer")
        async with connection.begin_nested() as savepoint:
            with pytest.raises(DBAPIError, match="invalid operator record query"):
                await connection.execute(
                    text(_QUERY),
                    {
                        "queue": "notifications",
                        "scope": "pending",
                        "offset": 0,
                        "limit": 10,
                        "record_id": reference,
                    },
                )
            await savepoint.rollback()
        await _role(connection, None)
        await connection.run_sync(lambda sync: replace(sync, forward=True))
        assert (await connection.execute(metadata, {"signature": _SIGNATURE})).one() == before
        repository = PostgresOperatorOperationsRepository(_scope(connection, "ec_operator_viewer"))
        exact = await repository.records(queue="notifications", record_id=reference)
        assert exact.total == 1 and exact.items[0].record_id == reference
