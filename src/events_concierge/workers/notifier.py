"""Notifier worker for the ADR-009 transactional outbox.

The worker is intentionally outside Temporal: lifecycle workflows append durable outbox rows, and this
small process uses PostgreSQL push wake-ups with a two-second durable-poll fallback. Cloud transport
remains behind ``NotificationPort`` so local/mock runs never require SES credentials.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ..application.notifier import NotifierWorker
from ..application.outbox import OutboxRelay
from ..composition import build_container
from ..config import get_settings
from ..infra.logging import configure_logging, get_logger
from ..ports.repositories import OutboxQueueSnapshot

_log = get_logger(__name__)


async def run_notifier() -> None:
    """Relay durable rows, draining backlog immediately and polling safely after idle wake-up loss."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    relay = OutboxRelay(
        container.outbox_repo,
        container.notifier,
        container.notification_secret_protector,
        lease_seconds=settings.outbox_lease_seconds,
        tenant_effect_authority=container.tenant_effect_authority,
        tenant_effect_timeout_seconds=settings.tenant_effect_timeout_seconds,
    )
    worker = NotifierWorker(
        relay,
        container.outbox_repo,
        container.outbox_wakeup,
        batch_size=settings.outbox_batch_size,
        poll_seconds=settings.outbox_poll_seconds,
    )
    try:
        health = await worker.start()
        _log.info(
            "notifier worker started",
            batch_size=settings.outbox_batch_size,
            poll_seconds=settings.outbox_poll_seconds,
            ready=health.ready,
            wakeup_mode=health.wakeup_mode.value,
        )
        while True:
            cycle = await worker.run_cycle()
            snapshot = cycle.queue_snapshot
            _log.info(
                "notifier relay cycle",
                duration_seconds=cycle.duration_seconds,
                ready=cycle.health.ready,
                wakeup_mode=cycle.health.wakeup_mode.value,
                wakeup_status=cycle.wakeup.status.value if cycle.wakeup is not None else None,
                queue_pending=snapshot.pending if snapshot is not None else None,
                queue_ready=snapshot.ready if snapshot is not None else None,
                queue_leased=snapshot.leased if snapshot is not None else None,
                oldest_ready_age_seconds=_oldest_ready_age_seconds(snapshot),
                relay_claimed=cycle.relay.claimed,
                relay_acknowledged=cycle.relay.acknowledged,
                notifier_port_sends=cycle.relay.sent,
                relay_retried=cycle.relay.retried,
                relay_failed=cycle.relay.failed,
                listener_failures=cycle.health.listener_failures,
                wakeups=cycle.health.wakeups,
                poll_fallbacks=cycle.health.poll_fallbacks,
                last_error=cycle.health.last_error,
            )
    finally:
        await worker.aclose()


def _oldest_ready_age_seconds(snapshot: OutboxQueueSnapshot | None) -> float | None:
    """Report queue lag from durable timestamps without reading a tenant payload (FR-8.9)."""
    if snapshot is None or snapshot.oldest_ready_at is None:
        return None
    return max((datetime.now(UTC) - snapshot.oldest_ready_at).total_seconds(), 0.0)


def main() -> None:
    asyncio.run(run_notifier())


if __name__ == "__main__":
    main()
