"""Disposable PostgreSQL backup/restore rehearsal for the local Compose stack.

The rehearsal never starts workers or connects the restored database to provider endpoints.  It
restores into a random ``ec_restore_drill_*`` database, compares durable aggregate counts, verifies
the migration head and forced-RLS posture, performs a two-tenant app-role isolation probe, then
drops the database.  The resulting report contains no rows, DSNs, credentials, or payloads.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory

_COMMAND_TIMEOUT_SECONDS = 300.0
_SOURCE_DATABASE = "ec"
_OWNER_ROLE = "ec"
_APPLICATION_ROLE = "ec_app"
_APPLICATION_PASSWORD = "ec_app"  # fixed local-Compose fixture only
_TENANT_A = "10000000-0000-0000-0000-000000000001"
_TENANT_B = "20000000-0000-0000-0000-000000000002"
_REQUEST_A = "10000000-0000-0000-0000-000000000011"
_REQUEST_B = "20000000-0000-0000-0000-000000000022"
_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_WRITER_SERVICES = frozenset(
    {
        "api",
        "account-erasure",
        "entity-intelligence",
        "ingestion-cadence",
        "ingestion-commands",
        "workflow-worker",
        "request-starter",
        "notifier",
        "change-delivery",
        "handoff-expiry",
        "lifecycle-invariants",
    }
)

_TABLE_INVENTORY_SQL = """
SELECT json_build_object(
    'schema_head', (SELECT version_num FROM public.alembic_version),
    'tables', (
        SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]'::json)
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public'
    ),
    'sequences', (
        SELECT COALESCE(json_agg(sequencename ORDER BY sequencename), '[]'::json)
        FROM pg_catalog.pg_sequences
        WHERE schemaname = 'public'
    )
)::text
""".strip()

_RLS_POSTURE_SQL = f"""
SELECT json_build_object(
    'app_role_exists', EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ),
    'app_role_superuser', COALESCE((
        SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ), true),
    'app_role_bypassrls', COALESCE((
        SELECT rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ), true),
    'app_role_createdb', COALESCE((
        SELECT rolcreatedb FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ), true),
    'app_role_createrole', COALESCE((
        SELECT rolcreaterole FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ), true),
    'app_role_replication', COALESCE((
        SELECT rolreplication FROM pg_catalog.pg_roles WHERE rolname = '{_APPLICATION_ROLE}'
    ), true),
    'app_role_memberships', (
        SELECT count(*)
        FROM pg_catalog.pg_auth_members membership
        JOIN pg_catalog.pg_roles member ON member.oid = membership.member
        WHERE member.rolname = '{_APPLICATION_ROLE}'
    ),
    'rls_tables', (
        SELECT count(*) FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
    ),
    'unforced_rls_tables', (
        SELECT count(*) FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relrowsecurity AND NOT c.relforcerowsecurity
    )
)::text
""".strip()

_INSERT_RLS_FIXTURES_SQL = f"""
INSERT INTO public.tenants (tenant_id, oidc_subject, notify_email, relay_inbox)
VALUES
    ('{_TENANT_A}', 'restore-drill-a', 'a@example.invalid', 'a@relay.invalid'),
    ('{_TENANT_B}', 'restore-drill-b', 'b@example.invalid', 'b@relay.invalid');
INSERT INTO public.event_requests (request_id, tenant_id, raw_text, constraints)
VALUES
    ('{_REQUEST_A}', '{_TENANT_A}', 'restore drill fixture a', '{{}}'::jsonb),
    ('{_REQUEST_B}', '{_TENANT_B}', 'restore drill fixture b', '{{}}'::jsonb);
""".strip()

_APP_RLS_PROBE_SQL = f"""
BEGIN;
SELECT set_config('app.tenant_id', '{_TENANT_A}', true);
SELECT json_build_object(
    'visible_fixture_rows', count(*),
    'cross_tenant_rows', count(*) FILTER (WHERE tenant_id = '{_TENANT_B}'::uuid)
)::text
FROM public.event_requests
WHERE request_id IN ('{_REQUEST_A}'::uuid, '{_REQUEST_B}'::uuid);
ROLLBACK;
""".strip()

type CommandRunner = Callable[[Sequence[str], bytes | None, float], subprocess.CompletedProcess[bytes]]


class _PreflightFailedError(RuntimeError):
    """Stop before backup after a named check already captured the safe failure."""


@dataclass(frozen=True, slots=True)
class RestoreCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class LocalRestoreDrillReport:
    """Credential-free evidence from one disposable local recovery rehearsal."""

    generated_at: str
    source_database: str
    restored_database: str
    migration_head: str | None
    backup_bytes: int
    backup_duration_seconds: float | None
    restore_duration_seconds: float | None
    total_duration_seconds: float
    restored_database_destroyed: bool
    checks: tuple[RestoreCheck, ...]

    @property
    def passed(self) -> bool:
        return self.restored_database_destroyed and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "events-concierge-local-restore-drill",
            "evidence_class": "local_recovery_rehearsal",
            "managed_recovery_eligible": False,
            "release_eligible": False,
            "generated_at": self.generated_at,
            "source_database": self.source_database,
            "restored_database": self.restored_database,
            "migration_head": self.migration_head,
            "backup_bytes": self.backup_bytes,
            "backup_duration_seconds": self.backup_duration_seconds,
            "restore_duration_seconds": self.restore_duration_seconds,
            "total_duration_seconds": self.total_duration_seconds,
            "restored_database_destroyed": self.restored_database_destroyed,
            "status": "passed" if self.passed else "failed",
            "checks": [asdict(check) for check in self.checks],
        }


def run_local_restore_drill(  # noqa: PLR0915 - explicit evidence steps mirror the runbook
    project_dir: Path,
    *,
    runner: CommandRunner | None = None,
) -> LocalRestoreDrillReport:
    """Dump, restore, validate, and destroy one isolated local database."""
    project_dir = project_dir.resolve()
    command_runner = runner or _run_command
    restored_database = f"ec_restore_drill_{datetime.now(UTC):%Y%m%d%H%M%S}_{uuid4().hex[:8]}"
    checks: list[RestoreCheck] = []
    started = monotonic()
    backup_bytes = 0
    backup_duration: float | None = None
    restore_duration: float | None = None
    migration_head: str | None = None
    restored_created = False
    restored_destroyed = False

    try:
        running_services = _running_services(project_dir, command_runner)
        active_writers = sorted(running_services & _WRITER_SERVICES)
        checks.append(
            RestoreCheck(
                "quiescent_local_source",
                not active_writers,
                "no local API/worker writer service is running"
                if not active_writers
                else "stop local API/worker services before the consistency rehearsal",
            )
        )
        if active_writers:
            raise _PreflightFailedError

        migration_heads = _migration_heads(project_dir)
        migration_head = migration_heads[0] if len(migration_heads) == 1 else None
        checks.append(
            RestoreCheck(
                "single_migration_head",
                migration_head is not None,
                "source tree has one migration head"
                if migration_head is not None
                else f"source tree has {len(migration_heads)} migration heads",
            )
        )
        if migration_head is None:
            raise _PreflightFailedError

        source_snapshot = _database_snapshot(
            project_dir, _SOURCE_DATABASE, command_runner
        )
        source_table_counts = source_snapshot.get("table_counts")
        source_sequence_states = source_snapshot.get("sequence_states")
        checks.append(
            RestoreCheck(
                "complete_source_inventory",
                isinstance(source_table_counts, dict)
                and bool(source_table_counts)
                and isinstance(source_sequence_states, dict),
                (
                    f"discovered {len(source_table_counts)} tables and "
                    f"{len(source_sequence_states)} sequences"
                    if isinstance(source_table_counts, dict)
                    and isinstance(source_sequence_states, dict)
                    else "source relation inventory is incomplete"
                ),
            )
        )
        checks.append(
            RestoreCheck(
                "source_at_migration_head",
                source_snapshot.get("schema_head") == migration_head,
                "source database is at the source-tree migration head"
                if source_snapshot.get("schema_head") == migration_head
                else "source database migration head does not match the source tree",
            )
        )
        if source_snapshot.get("schema_head") != migration_head:
            raise _PreflightFailedError

        backup_started = monotonic()
        dump = _checked(
            command_runner,
            _compose_exec(
                project_dir,
                "postgres",
                "pg_dump",
                "-U",
                _OWNER_ROLE,
                "-d",
                _SOURCE_DATABASE,
                "--format=custom",
                "--no-owner",
            ),
            step="database backup",
        ).stdout
        backup_duration = monotonic() - backup_started
        backup_bytes = len(dump)
        checks.append(
            RestoreCheck(
                "backup_artifact",
                backup_bytes > 0,
                "pg_dump produced a non-empty custom-format artifact"
                if backup_bytes > 0
                else "pg_dump produced an empty artifact",
            )
        )

        _checked(
            command_runner,
            _compose_exec(
                project_dir,
                "postgres",
                "createdb",
                "-U",
                _OWNER_ROLE,
                "--template=template0",
                restored_database,
            ),
            step="create isolated restore database",
        )
        restored_created = True

        restore_started = monotonic()
        _checked(
            command_runner,
            _compose_exec(
                project_dir,
                "postgres",
                "pg_restore",
                "-U",
                _OWNER_ROLE,
                "-d",
                restored_database,
                "--no-owner",
                "--exit-on-error",
            ),
            input_data=dump,
            step="database restore",
        )
        restore_duration = monotonic() - restore_started

        restored_snapshot = _database_snapshot(
            project_dir, restored_database, command_runner
        )
        checks.append(
            RestoreCheck(
                "restored_migration_head",
                restored_snapshot.get("schema_head") == migration_head,
                "restored schema matches the source-tree migration head"
                if restored_snapshot.get("schema_head") == migration_head
                else "restored schema does not match the source-tree migration head",
            )
        )
        checks.append(
            RestoreCheck(
                "durable_aggregate_continuity",
                restored_snapshot == source_snapshot,
                "sanitized durable aggregate counts match the source backup"
                if restored_snapshot == source_snapshot
                else "restored durable aggregate counts differ from the source backup",
            )
        )

        rls_posture = _json_query(
            project_dir,
            restored_database,
            _OWNER_ROLE,
            _RLS_POSTURE_SQL,
            command_runner,
        )
        rls_tables = rls_posture.get("rls_tables")
        unforced_rls_tables = rls_posture.get("unforced_rls_tables")
        app_role_memberships = rls_posture.get("app_role_memberships")
        posture_passed = (
            rls_posture.get("app_role_exists") is True
            and rls_posture.get("app_role_superuser") is False
            and rls_posture.get("app_role_bypassrls") is False
            and rls_posture.get("app_role_createdb") is False
            and rls_posture.get("app_role_createrole") is False
            and rls_posture.get("app_role_replication") is False
            and type(app_role_memberships) is int
            and app_role_memberships == 0
            and type(rls_tables) is int
            and rls_tables > 0
            and type(unforced_rls_tables) is int
            and unforced_rls_tables == 0
        )
        checks.append(
            RestoreCheck(
                "restored_rls_posture",
                posture_passed,
                "app role has no elevated attributes/memberships and every RLS table is forced"
                if posture_passed
                else "restored role/RLS posture is unsafe",
            )
        )

        _checked(
            command_runner,
            _psql_command(
                project_dir,
                restored_database,
                _OWNER_ROLE,
                _INSERT_RLS_FIXTURES_SQL,
            ),
            step="insert isolated RLS drill fixtures",
        )
        app_probe = _json_query(
            project_dir,
            restored_database,
            _APPLICATION_ROLE,
            _APP_RLS_PROBE_SQL,
            command_runner,
            local_app_password=True,
        )
        isolation_passed = (
            app_probe.get("visible_fixture_rows") == 1
            and app_probe.get("cross_tenant_rows") == 0
        )
        checks.append(
            RestoreCheck(
                "restored_app_role_tenant_isolation",
                isolation_passed,
                "restored app role sees its tenant fixture and no cross-tenant fixture"
                if isolation_passed
                else "restored app role tenant-isolation probe failed",
            )
        )
    except _PreflightFailedError:
        pass
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
        checks.append(
            RestoreCheck(
                "drill_execution",
                False,
                f"restore rehearsal failed closed ({type(error).__name__})",
            )
        )
    finally:
        if restored_created:
            try:
                result = command_runner(
                    _compose_exec(
                        project_dir,
                        "postgres",
                        "dropdb",
                        "-U",
                        _OWNER_ROLE,
                        "--if-exists",
                        "--force",
                        restored_database,
                    ),
                    None,
                    _COMMAND_TIMEOUT_SECONDS,
                )
            except (OSError, RuntimeError, subprocess.SubprocessError):
                restored_destroyed = False
            else:
                restored_destroyed = result.returncode == 0
        else:
            restored_destroyed = True
        checks.append(
            RestoreCheck(
                "isolated_restore_destroyed",
                restored_destroyed,
                "isolated restored database was destroyed"
                if restored_destroyed
                else "isolated restored database cleanup failed",
            )
        )

    return _report(
        started,
        restored_database,
        migration_head,
        backup_bytes,
        backup_duration,
        restore_duration,
        restored_destroyed,
        checks,
    )


def _migration_heads(project_dir: Path) -> list[str]:
    config = Config(str(project_dir / "alembic.ini"))
    config.set_main_option("script_location", str(project_dir / "migrations"))
    return list(ScriptDirectory.from_config(config).get_heads())


def _database_snapshot(
    project_dir: Path,
    database: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    inventory = _json_query(
        project_dir,
        database,
        _OWNER_ROLE,
        _TABLE_INVENTORY_SQL,
        runner,
    )
    schema_head = inventory.get("schema_head")
    raw_tables = inventory.get("tables")
    raw_sequences = inventory.get("sequences")
    if (
        not isinstance(schema_head, str)
        or not isinstance(raw_tables, list)
        or not isinstance(raw_sequences, list)
    ):
        raise ValueError("database inventory has an invalid shape")
    tables = _validated_relation_names(raw_tables, require_nonempty=True)
    sequences = _validated_relation_names(raw_sequences, require_nonempty=False)
    counts = {
        table: _table_count(project_dir, database, table, runner) for table in tables
    }
    sequence_states = {
        sequence: _sequence_state(project_dir, database, sequence, runner)
        for sequence in sequences
    }
    return {
        "schema_head": schema_head,
        "table_counts": counts,
        "sequence_states": sequence_states,
    }


def _validated_relation_names(values: list[object], *, require_nonempty: bool) -> list[str]:
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _TABLE_NAME.fullmatch(value):
            raise ValueError("database inventory contains an unsafe relation name")
        names.append(value)
    if (
        (require_nonempty and not names)
        or len(names) != len(set(names))
        or names != sorted(names)
    ):
        raise ValueError("database inventory is empty, duplicated, or unordered")
    return names


def _table_count(
    project_dir: Path,
    database: str,
    table: str,
    runner: CommandRunner,
) -> int:
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError("unsafe table name")
    result = _checked(
        runner,
        _psql_command(
            project_dir,
            database,
            _OWNER_ROLE,
            f'SELECT count(*)::bigint FROM public."{table}"',
        ),
        step="sanitized table-count query",
    )
    lines = [line.strip() for line in result.stdout.decode().splitlines() if line.strip()]
    if not lines:
        raise ValueError("table-count query returned no result")
    try:
        count = int(lines[-1])
    except ValueError as error:
        raise ValueError("table-count query returned a non-integer") from error
    if count < 0:
        raise ValueError("table-count query returned a negative value")
    return count


def _sequence_state(
    project_dir: Path,
    database: str,
    sequence: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    if not _TABLE_NAME.fullmatch(sequence):
        raise ValueError("unsafe sequence name")
    state = _json_query(
        project_dir,
        database,
        _OWNER_ROLE,
        (
            "SELECT json_build_object('last_value', last_value, 'is_called', is_called)::text "
            f'FROM public."{sequence}"'
        ),
        runner,
    )
    last_value = state.get("last_value")
    if type(last_value) is not int or type(state.get("is_called")) is not bool:
        raise ValueError("sequence-state query returned an invalid shape")
    return state


def _running_services(project_dir: Path, runner: CommandRunner) -> set[str]:
    result = _checked(
        runner,
        [
            "docker",
            "compose",
            "--project-directory",
            str(project_dir),
            "-f",
            str(project_dir / "docker-compose.yml"),
            "--profile",
            "app",
            "ps",
            "--services",
            "--filter",
            "status=running",
        ],
        step="Compose writer preflight",
    )
    return {line.strip() for line in result.stdout.decode().splitlines() if line.strip()}


def _json_query(
    project_dir: Path,
    database: str,
    user: str,
    query: str,
    runner: CommandRunner,
    *,
    local_app_password: bool = False,
) -> dict[str, Any]:
    command = _psql_command(project_dir, database, user, query)
    if local_app_password:
        service_index = command.index("postgres")
        command[service_index:service_index] = ["-e", f"PGPASSWORD={_APPLICATION_PASSWORD}"]
    result = _checked(runner, command, step="sanitized database validation query")
    lines = [line.strip() for line in result.stdout.decode().splitlines() if line.strip()]
    if not lines:
        raise ValueError("database validation query returned no result")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise ValueError("database validation query returned a non-object")
    return value


def _psql_command(project_dir: Path, database: str, user: str, query: str) -> list[str]:
    return _compose_exec(
        project_dir,
        "postgres",
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        user,
        "-d",
        database,
        "-c",
        query,
    )


def _compose_exec(project_dir: Path, service: str, *command: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(project_dir),
        "-f",
        str(project_dir / "docker-compose.yml"),
        "exec",
        "-T",
        service,
        *command,
    ]


def _checked(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    input_data: bytes | None = None,
    step: str,
) -> subprocess.CompletedProcess[bytes]:
    result = runner(command, input_data, _COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(f"{step} failed with exit code {result.returncode}")
    return result


def _run_command(
    command: Sequence[str],
    input_data: bytes | None,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        input=input_data,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _report(
    started: float,
    restored_database: str,
    migration_head: str | None,
    backup_bytes: int,
    backup_duration: float | None,
    restore_duration: float | None,
    restored_destroyed: bool,
    checks: list[RestoreCheck],
) -> LocalRestoreDrillReport:
    return LocalRestoreDrillReport(
        generated_at=datetime.now(UTC).isoformat(),
        source_database=_SOURCE_DATABASE,
        restored_database=restored_database,
        migration_head=migration_head,
        backup_bytes=backup_bytes,
        backup_duration_seconds=(round(backup_duration, 6) if backup_duration is not None else None),
        restore_duration_seconds=(
            round(restore_duration, 6) if restore_duration is not None else None
        ),
        total_duration_seconds=round(monotonic() - started, 6),
        restored_database_destroyed=restored_destroyed,
        checks=tuple(checks),
    )
