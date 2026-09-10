"""Manual catalog entrypoints verify executor authority before source or Temporal work."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from events_concierge.config import Settings
from events_concierge.workers import catalog_refresh, catalog_refresh_dispatcher


@pytest.mark.parametrize("worker", [catalog_refresh, catalog_refresh_dispatcher])
async def test_catalog_cli_checks_executor_role_before_building_any_egress_router(
    monkeypatch: pytest.MonkeyPatch, worker,
) -> None:
    settings = Settings(_env_file=None)
    container = object()
    build = Mock(return_value=container)
    verify = AsyncMock(side_effect=ValueError("executor authority required"))
    router = AsyncMock()
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", Mock())
    monkeypatch.setattr(worker, "build_catalog_container", build)
    monkeypatch.setattr(worker, "verify_catalog_executor_database", verify)
    monkeypatch.setattr(worker, "build_catalog_refresh_router", router)

    with pytest.raises(ValueError, match="executor authority required"):
        if worker is catalog_refresh:
            await worker.refresh_once("approved-calendar", "manual:authority")
        else:
            await worker.dispatch_once()

    build.assert_called_once_with(settings)
    verify.assert_awaited_once_with()
    router.assert_not_awaited()
