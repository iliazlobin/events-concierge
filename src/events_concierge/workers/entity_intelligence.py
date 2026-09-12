"""Continuously refresh bounded public facts for exact event-entity identities.

A cycle failure is logged with its first line and with how many cycles have failed in a row.  Both
matter: this loop once spent hours emitting ``entity intelligence cycle failed
error_type=ProgrammingError`` every thirty seconds because a deployed image still called
``fn_get_catalog_entity_insights_v1`` after a migration dropped it.  The name of the missing
function was in the exception the whole time and never reached the log, and a permanent
code-to-schema drift was indistinguishable from one bad poll.

**Only the first line.**  The sibling poll loops log the exception *type* alone, and say why:
"Database exception messages can contain operational details."  They are right, and here it is
sharper than operational -- a DBAPI error from this lane renders the statement it was running,
and those statements bind a third party's display name and public profile URL.  A single
constraint violation would put a named individual's LinkedIn URL into stdout on every poll,
undeduplicated, forever.  SQLAlchemy puts the diagnosis on line one and the ``[SQL: …]`` and
``[parameters: …]`` renderings on the lines after it, so the first line carries the missing
function's name and none of the bound values.
"""

from __future__ import annotations

import asyncio

from ..composition import build_container
from ..config import get_settings
from ..deployment.startup import preflight_application_runtime
from ..infra.logging import configure_logging, get_logger

_log = get_logger(__name__)

#: Consecutive failed cycles after which the loop stops treating the failure as transient.
_PERSISTENT_FAILURE_CYCLES = 5

#: Enough for a diagnosis, short enough that no rendered row can ride along behind one.
_MAX_DIAGNOSTIC_CHARS = 300


def _diagnostic(error: Exception) -> str:
    """Return the exception's first line only, bounded.

    SQLAlchemy renders ``(orig type) message`` on line one and the statement and its bound
    parameters on the lines below, so this names what failed without carrying anyone's data.
    """
    return str(error).split("\n", 1)[0].strip()[:_MAX_DIAGNOSTIC_CHARS]


async def run_entity_intelligence() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    if not settings.entity_intelligence_enabled:
        _log.info("entity intelligence worker disabled")
        return
    container = build_container(settings, runtime_ports=preflight_application_runtime(settings))
    _log.info(
        "entity intelligence worker started",
        batch_size=settings.entity_intelligence_batch_size,
        poll_seconds=settings.entity_intelligence_poll_seconds,
    )
    consecutive_failures = 0
    while True:
        try:
            refreshed = await container.entity_intelligence.refresh_due(
                settings.entity_intelligence_batch_size
            )
        except Exception as error:
            consecutive_failures += 1
            # A run of identical failures is a drift, not a blip, and it must not stay a warning
            # forever: nothing else in this loop will ever escalate on its own.
            log = _log.error if consecutive_failures >= _PERSISTENT_FAILURE_CYCLES else _log.warning
            log(
                "entity intelligence cycle failed",
                error_type=type(error).__name__,
                error=_diagnostic(error),
                consecutive_failures=consecutive_failures,
            )
            refreshed = []
        else:
            consecutive_failures = 0
        if refreshed:
            _log.info("entity intelligence refreshed", entity_count=len(refreshed))
        await asyncio.sleep(settings.entity_intelligence_poll_seconds)


def main() -> None:
    asyncio.run(run_entity_intelligence())


if __name__ == "__main__":
    main()
