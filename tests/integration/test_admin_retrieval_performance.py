"""Compact operator reads remain independent of catalog work and preserve authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source
from tests.integration.test_operator_run_queries import _run

pytestmark = pytest.mark.integration


async def test_navigation_reads_do_not_wait_for_locked_catalog_inventory() -> None:
    # This is an execution-path regression check, not a machine-speed threshold.
    # A writer holding the catalog must not prevent rendering navigation/policy.
    async with _owner_transaction() as catalog_writer:
        await catalog_writer.execute(
            text(
                "LOCK public.canonical_events,public.catalog_event_observations IN ACCESS EXCLUSIVE MODE"
            )
        )
        async with _owner_transaction() as reader:
            await reader.execute(text("SET LOCAL statement_timeout='1500ms'"))
            await _role(reader, "ec_operator_viewer")
            policy = (
                await reader.execute(
                    text("SELECT * FROM public.fn_get_ingestion_admin_policy_v1()")
                )
            ).one()
            assert policy.code in {
                "allowed",
                "modality_disabled",
                "source_quarantined",
                "unknown_source",
            }
            rows = (
                (
                    await reader.execute(
                        text("SELECT * FROM public.fn_list_ingestion_admin_source_status_v1(false)")
                    )
                )
                .mappings()
                .all()
            )
            assert all(not row["source_key"].startswith("test-") for row in rows)
            await reader.execute(
                text(
                    "SELECT * FROM public.fn_list_ingestion_admin_filter_values_v2(NULL,'all',NULL,NULL,NULL,false)"
                )
            )
            await reader.execute(
                text(
                    "SELECT public.fn_count_ingestion_admin_sources_v2(NULL,'all',NULL,NULL,NULL,NULL,false)"
                )
            )


async def test_compact_policy_and_source_status_keep_existing_semantics_and_grants() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        retired = await _source(connection)
        fixture = await _source(connection)
        await connection.execute(
            text(
                "UPDATE public.catalog_sources SET retired_at=statement_timestamp(),enabled=false,retired_reason='Retired' WHERE source_key=:key"
            ),
            {"key": retired},
        )
        await connection.execute(
            text("UPDATE public.catalog_sources SET publisher='Tests' WHERE source_key=:key"),
            {"key": fixture},
        )
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            for include_fixtures in (False, True):
                roster = {
                    row["source_key"]: row
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT * FROM public.fn_list_ingestion_admin_source_status_v1(:fixtures)"
                            ),
                            {"fixtures": include_fixtures},
                        )
                    ).mappings()
                }
                assert (fixture in roster) is include_fixtures
                for key in (source, retired):
                    full = (
                        (
                            await connection.execute(
                                text(
                                    "SELECT * FROM public.fn_list_ingestion_admin_sources_v7(NULL,'all',NULL,NULL,NULL,:key,:fixtures,'source','asc',1,0)"
                                ),
                                {"key": key, "fixtures": include_fixtures},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    for field in roster[key]:
                        if field != "refresh_interval_minutes":
                            assert roster[key][field] == full[field]
                assert roster[retired]["effective_status"] == "retired"
                assert not roster[retired]["enabled"] and not roster[retired]["due"]
            await _denied(
                connection, "SELECT * FROM public.fn_ingestion_admin_source_states_v1(false)"
            )
            await _denied(connection, "SELECT * FROM public.catalog_sources")
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, "SELECT * FROM public.fn_get_ingestion_admin_policy_v1()")
            await _denied(
                connection, "SELECT * FROM public.fn_list_ingestion_admin_source_status_v1(false)"
            )
        await _role(connection, None)
        for quarantined, allowed in ((False, True), (False, False), (True, True)):
            await connection.execute(
                text(
                    "UPDATE public.source_policy SET quarantined=:quarantine,automation_allowed=jsonb_set(automation_allowed,'{browser}',to_jsonb(CAST(:allowed AS boolean))) WHERE source='public_jsonld'"
                ),
                {"quarantine": quarantined, "allowed": allowed},
            )
            policy = (
                (
                    await connection.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_policy_v1()")
                    )
                )
                .mappings()
                .one()
            )
            overview = (
                (
                    await connection.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_overview_v2()")
                    )
                )
                .mappings()
                .one()
            )
            assert dict(policy) == {
                field: overview[f"policy_{field}"] for field in ("allowed", "reason", "code")
            }


async def test_run_evidence_matches_pre_optimization_with_fixture_and_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module("migrations.versions.0196_admin_retrieval_performance")
    async with _owner_transaction() as connection:
        source = await _source(connection)
        now = datetime.now(UTC) - timedelta(hours=1)
        for suffix, delta, status, error in (
            ("failed", 0, "failed", "timeout private provider details"),
            ("success-a", 1, "succeeded", None),
            ("success-b", 1, "succeeded", None),
            ("fixture", 2, "succeeded", "fixture run"),
            ("expired", 3, "running", None),
        ):
            await _run(
                connection,
                source,
                f"manual:{uuid4()}:{suffix}",
                now + timedelta(minutes=delta),
                status=status,
                error=error,
            )

        def apply(sync: Connection, action: str) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            getattr(migration, action)()

        query = text(
            "SELECT public.fn_query_ingestion_admin_runs_v1(jsonb_build_object('source_key',CAST(:source AS text),'include_fixtures',CAST(:fixtures AS boolean)))"
        )
        sources = text(
            "SELECT * FROM public.fn_list_ingestion_admin_sources_v7(NULL,'all',NULL,NULL,NULL,:source,:fixtures,'source','asc',1,0)"
        )
        optimized_sources = [
            (await connection.execute(sources, {"source": source, "fixtures": fixtures}))
            .mappings()
            .one()
            for fixtures in (False, True)
        ]
        optimized = [
            (await connection.execute(query, {"source": source, "fixtures": fixtures})).scalar_one()
            for fixtures in (False, True)
        ]
        assert all(
            {"collection_window", "execution_configuration", "collection_horizon_days"}
            <= item.keys()
            for result in optimized
            for item in result["items"]
        )
        await connection.run_sync(apply, "downgrade")
        original_sources = [
            (await connection.execute(sources, {"source": source, "fixtures": fixtures}))
            .mappings()
            .one()
            for fixtures in (False, True)
        ]
        original = [
            (await connection.execute(query, {"source": source, "fixtures": fixtures})).scalar_one()
            for fixtures in (False, True)
        ]
        assert optimized == original
        assert optimized_sources == original_sources
        await connection.run_sync(apply, "upgrade")
        restored = [
            (await connection.execute(query, {"source": source, "fixtures": fixtures})).scalar_one()
            for fixtures in (False, True)
        ]
        assert restored == original


@pytest.mark.parametrize("own_grant", [False, True])
async def test_replacing_definer_projection_preserves_noinherit_membership_options(
    monkeypatch: pytest.MonkeyPatch, own_grant: bool
) -> None:
    migration = import_module("migrations.versions.0196_admin_retrieval_performance")
    role = f"ec_perf_migration_{uuid4().hex[:12]}"
    async with _owner_transaction() as connection:
        await connection.execute(
            text(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOINHERIT NOBYPASSRLS"
            )
        )
        await connection.execute(text(f"GRANT USAGE,CREATE ON SCHEMA public TO {role}"))
        await connection.execute(
            text(f"GRANT ec_operator_aggregate_definer TO {role} WITH ADMIN TRUE")
        )
        await connection.execute(
            text(f"GRANT ec_operator_aggregate_definer TO {role} WITH INHERIT FALSE")
        )
        await connection.execute(text(f"SET LOCAL ROLE {role}"))
        if own_grant:
            await connection.execute(
                text(
                    f"GRANT ec_operator_aggregate_definer TO {role} WITH INHERIT FALSE GRANTED BY {role}"
                )
            )
            await connection.execute(
                text(
                    f"GRANT ec_operator_aggregate_definer TO {role} WITH SET FALSE GRANTED BY {role}"
                )
            )
        membership = text("""
            SELECT target.rolname,member.rolname,grantor.rolname,
                   membership.admin_option,membership.inherit_option,membership.set_option
            FROM pg_auth_members membership
            JOIN pg_roles target ON target.oid=membership.roleid
            JOIN pg_roles member ON member.oid=membership.member
            JOIN pg_roles grantor ON grantor.oid=membership.grantor
            WHERE target.rolname='ec_operator_aggregate_definer'
            ORDER BY 1,2,3
        """)
        before = (await connection.execute(membership)).all()
        assert not (
            await connection.execute(
                text("SELECT pg_has_role(current_user,'ec_operator_aggregate_definer','USAGE')")
            )
        ).scalar_one()

        def replace(sync: Connection) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            with migration._aggregate_owner():
                # Prove this is enough authority to perform the actual replacement,
                # not just a membership flag that looks plausible.
                sync.execute(text(migration._SOURCE_SQL))

        # Both forward and rollback replacement scopes must restore every grantor
        # and ADMIN/INHERIT/SET option, including a pre-existing self-issued grant.
        for _ in range(2):
            await connection.run_sync(replace)
            assert (await connection.execute(membership)).all() == before
            assert not (
                await connection.execute(
                    text("SELECT pg_has_role(current_user,'ec_operator_aggregate_definer','USAGE')")
                )
            ).scalar_one()
