"""Retained operator diagnostics keep their scope and database privacy boundary."""

import pytest
from sqlalchemy import text
from tests.integration.test_operator_management import _denied, _owner_transaction, _role
from tests.integration.test_operator_work_records import _notification, _tenant

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "message",
    [
        "Provider failed token=private-token",
        "Timeout\nAuthorization: Bearer private-token\nRetry later",
        "Provider postgresql+psycopg://user:private-token@host/database failed",
    ],
)
async def test_retained_diagnostic_redacts_before_restricted_role_reads(message: str) -> None:
    async with _owner_transaction(empty_queues=True) as connection:
        tenant = await _tenant(connection)
        reference = await _notification(connection, tenant, state="scheduled", error=message)
        query = text(
            "SELECT public.fn_get_operator_record_diagnostic_v1('notifications',:scope,:id)"
        )
        await _role(connection, "ec_operator_viewer")
        result = (
            await connection.execute(query, {"scope": "pending", "id": reference})
        ).scalar_one()
        assert result["record_id"] == reference
        assert result["redacted"] and "private-token" not in result["message"]
        assert (
            await connection.execute(query, {"scope": "failed", "id": reference})
        ).scalar_one() is None
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT public.fn_get_operator_record_diagnostic_v1('notifications','pending','1')",
            )
