"""Real login/bootstrap checks on isolated databases and UUID-scoped test roles.

The fixed production allowlist is substituted only in this test process. No ec_app,
local/development reserved login, or existing capability membership is modified.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from events_concierge.deployment import operator_logins
from events_concierge.deployment.development_operator_bootstrap import verify_login_passwords
from events_concierge.deployment.operator_logins import (
    Credentials,
    bootstrap_operator_logins,
    preflight_operator_logins,
)

pytestmark = pytest.mark.integration


@dataclass
class LoginFixture:
    engine: Engine
    url: str
    operator: str
    executor: str
    controller: str
    execution: str
    consumer: str
    unrelated: str
    credentials: Credentials

    def execute(self, statement: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(statement))

    def exists(self, role: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                    {"role": role},
                ).scalar_one()
            )

    def membership(self, member: str, role: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT pg_has_role(:member,:role,'MEMBER')"),
                    {"member": member, "role": role},
                ).scalar_one()
            )


@pytest.fixture
def logins(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoginFixture]:
    url = os.environ.get("EC_MIGRATION_URL")
    if not url:
        pytest.skip("EC_MIGRATION_URL not set; use the isolated integration runner")
    engine = create_engine(url, hide_parameters=True)
    prefix = "ec_bootstrap_test_" + uuid4().hex[:12]
    operator, executor, controller, execution, consumer, unrelated = (
        prefix + suffix for suffix in ("_op", "_exec", "_ctrl", "_cap", "_consumer", "_other")
    )
    credentials = (
        (operator, controller, "operator-fixture-" + uuid4().hex),
        (executor, execution, "executor-fixture-" + uuid4().hex),
    )
    fixture = LoginFixture(
        engine, url, operator, executor, controller, execution, consumer, unrelated, credentials
    )
    monkeypatch.setattr(
        operator_logins,
        "_ALLOWED_LOGINS",
        {
            operator: controller,
            executor: execution,
        },
    )
    try:
        for role in (controller, execution, consumer, unrelated):
            fixture.execute(f"CREATE ROLE {role} NOLOGIN")
        yield fixture
    finally:
        # Names are generated here, not discovered from shared cluster state.
        with engine.begin() as connection:
            for role in (operator, executor, controller, execution, consumer, unrelated):
                connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
        engine.dispose()


def test_inbound_consumer_membership_cannot_gain_controller_authority(logins: LoginFixture) -> None:
    logins.execute(f"CREATE ROLE {logins.operator} LOGIN")
    logins.execute(f"GRANT {logins.operator} TO {logins.consumer}")
    with pytest.raises(RuntimeError, match="collides"):
        preflight_operator_logins(logins.engine, logins.credentials)
    with pytest.raises(RuntimeError, match="collides"):
        bootstrap_operator_logins(logins.engine, logins.credentials)
    assert not logins.membership(logins.consumer, logins.controller)
    assert not logins.exists(logins.executor)


def test_unexpected_ordinary_inherited_role_is_rejected(logins: LoginFixture) -> None:
    logins.execute(f"CREATE ROLE {logins.operator} LOGIN")
    logins.execute(f"GRANT {logins.unrelated} TO {logins.operator}")
    with pytest.raises(RuntimeError, match="collides"):
        preflight_operator_logins(logins.engine, logins.credentials)
    with pytest.raises(RuntimeError, match="collides"):
        bootstrap_operator_logins(logins.engine, logins.credentials)
    assert not logins.membership(logins.operator, logins.controller)


def test_second_login_collision_rolls_back_first_creation_and_grant(logins: LoginFixture) -> None:
    logins.execute(f"CREATE ROLE {logins.executor} LOGIN NOINHERIT")
    with pytest.raises(RuntimeError, match="collides"):
        bootstrap_operator_logins(logins.engine, logins.credentials)
    assert not logins.exists(logins.operator)
    assert logins.exists(logins.executor)
    assert not logins.membership(logins.executor, logins.execution)


def test_repeated_bootstrap_preserves_password_and_authenticates_directly(
    logins: LoginFixture,
) -> None:
    assert preflight_operator_logins(logins.engine, logins.credentials) == ()
    bootstrap_operator_logins(logins.engine, logins.credentials)
    verify_login_passwords(logins.url, logins.credentials)
    assert preflight_operator_logins(logins.engine, logins.credentials) == (
        logins.operator,
        logins.executor,
    )
    changed = tuple(
        (login, capability, password + "-changed")
        for login, capability, password in logins.credentials
    )
    bootstrap_operator_logins(logins.engine, changed)
    verify_login_passwords(logins.url, logins.credentials)
    with pytest.raises(RuntimeError, match="login verification failed") as error:
        verify_login_passwords(logins.url, changed)
    assert all(password not in str(error.value) for _, _, password in changed)
    assert logins.membership(logins.operator, logins.controller)
    assert logins.membership(logins.executor, logins.execution)
    assert not logins.membership(logins.operator, logins.execution)
    assert not logins.membership(logins.executor, logins.controller)


def test_preflight_does_not_require_migrated_capability_roles(logins: LoginFixture) -> None:
    logins.execute(f"DROP ROLE {logins.controller}")
    logins.execute(f"DROP ROLE {logins.execution}")
    assert preflight_operator_logins(logins.engine, logins.credentials) == ()
    logins.execute(f"CREATE ROLE {logins.operator} LOGIN")
    assert preflight_operator_logins(logins.engine, logins.credentials) == (logins.operator,)
