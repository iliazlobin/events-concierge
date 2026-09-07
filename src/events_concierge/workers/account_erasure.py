"""Durable FR-10.5 account-erasure resume worker.

The authenticated API installs the tenant fence and may optimistically drive cleanup. This
process is the independent convergence owner: it leases pending tombstones, retries only fixed
external families, and logs aggregate counts without tenant or provider detail.
"""

from __future__ import annotations

import asyncio

from ..adapters.postgres.tenant_effects import PostgresTenantExternalEffectDrain
from ..application.account_erasure import AccountErasureService, AccountErasureWorker
from ..composition import build_container
from ..config import get_settings
from ..infra.logging import configure_logging, get_logger
from ..workflows.account_erasure import (
    NoopTenantSessionRevocation,
    TemporalTenantWorkflowCancellation,
)
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


async def run_account_erasure() -> None:
    """Continuously converge due erasures; durable leases recover process or network failure."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    temporal = await connect_temporal(
        settings,
        container.object_store,
        lazy=True,
        tenant_effect_authority=container.tenant_effect_authority,
    )
    service = AccountErasureService(
        container.account_erasure_repo,
        PostgresTenantExternalEffectDrain(
            container.tenant_effect_authority,
            timeout_seconds=settings.tenant_effect_timeout_seconds,
        ),
        TemporalTenantWorkflowCancellation(
            temporal,
            rpc_timeout_seconds=settings.temporal_rpc_timeout_seconds,
        ),
        container.calendar,
        container.browser_session or NoopTenantSessionRevocation(),
        container.vault,
        container.object_store,
        container.media_store,
    )
    worker = AccountErasureWorker(
        container.account_erasure_repo,
        service,
        lease_seconds=settings.account_erasure_lease_seconds,
    )
    _log.info(
        "account erasure worker started",
        batch_size=settings.account_erasure_batch_size,
        poll_seconds=settings.account_erasure_poll_seconds,
    )
    while True:
        try:
            stats = await worker.run_once(limit=settings.account_erasure_batch_size)
        except Exception as error:
            _log.warning(
                "account erasure worker pass failed",
                error_type=type(error).__name__,
            )
            await asyncio.sleep(settings.account_erasure_poll_seconds)
            continue
        _log.info(
            "account erasure worker cycle",
            claimed=stats.claimed,
            completed=stats.completed,
            rescheduled=stats.rescheduled,
            lost_leases=stats.lost_leases,
        )
        if stats.claimed == 0:
            await asyncio.sleep(settings.account_erasure_poll_seconds)


def main() -> None:
    asyncio.run(run_account_erasure())


if __name__ == "__main__":
    main()
