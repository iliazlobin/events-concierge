"""Application-role password rotation is explicit, bounded, and emits no secret evidence."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType

import pytest

from events_concierge.operations import app_role


class _Result:
    def __init__(self, *, mapping: dict[str, object] | None = None, scalar: object = None) -> None:
        self._mapping = mapping
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._mapping

    def scalar_one(self) -> object:
        return self._scalar


class _Connection:
    def __init__(self, *, elevated: bool = False, memberships: int = 0) -> None:
        self.elevated = elevated
        self.memberships = memberships
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _Result:
        rendered = str(statement)
        self.calls.append((rendered, parameters))
        if "AS role_memberships" in rendered:
            return _Result(
                mapping={
                    "rolcanlogin": False,
                    "rolsuper": self.elevated,
                    "rolcreatedb": False,
                    "rolcreaterole": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                    "role_memberships": self.memberships,
                }
            )
        if "SELECT rolcanlogin FROM" in rendered:
            return _Result(scalar=True)
        return _Result()


class _Begin(AbstractContextManager[_Connection]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.disposed = False

    def begin(self) -> _Begin:
        return _Begin(self.connection)

    def dispose(self) -> None:
        self.disposed = True


def test_rotation_stages_secret_as_a_bind_parameter_and_emits_sanitized_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    engine = _Engine(connection)
    secret = "fixture-long-random-password"
    monkeypatch.setattr(app_role, "create_engine", lambda *args, **kwargs: engine)

    report = app_role.rotate_app_role_password(
        "postgresql+psycopg://owner:fixture@database.example/events",
        secret,
    )

    assert report.passed is True
    assert engine.disposed is True
    assert secret not in str(report.to_dict())
    assert all(secret not in statement for statement, _ in connection.calls)
    assert any(parameters == {"password": secret} for _, parameters in connection.calls)
    assert any(
        "ALTER ROLE ec_app LOGIN PASSWORD %L" in statement for statement, _ in connection.calls
    )


def test_rotation_refuses_an_elevated_application_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(elevated=True)
    engine = _Engine(connection)
    monkeypatch.setattr(app_role, "create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(RuntimeError, match="elevated attributes"):
        app_role.rotate_app_role_password("postgresql+psycopg://owner@database/events", "secret")

    assert engine.disposed is True
    assert not any(parameters == {"password": "secret"} for _, parameters in connection.calls)


def test_rotation_refuses_application_role_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(memberships=1)
    engine = _Engine(connection)
    monkeypatch.setattr(app_role, "create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(RuntimeError, match="role memberships"):
        app_role.rotate_app_role_password(
            "postgresql+psycopg://owner@database/events",
            "secret",
        )

    assert engine.disposed is True
    assert not any(parameters == {"password": "secret"} for _, parameters in connection.calls)


@pytest.mark.parametrize("password", ["", "bad\x00secret", "x" * 1025])
def test_rotation_rejects_invalid_secret_material(password: str) -> None:
    with pytest.raises(ValueError, match="EC_APP_ROLE_PASSWORD"):
        app_role.validate_app_role_password(password)
