"""Read-only ADR-007 lifecycle divergence and nightly-invariant scanner."""

from __future__ import annotations

from ..domain.invariants import LifecycleInvariantReport
from ..ports.invariants import LifecycleInvariantRepository
from ..ports.workflows import WorkflowLivenessInspector

_MAX_WORKFLOW_BATCH_SIZE = 1000


class LifecycleInvariantScanner:
    """Combine PostgreSQL hygiene counts with authoritative Temporal liveness without remediation.

    This scanner is deliberately observational: it never writes lifecycle/watch/handoff state and
    never turns a liveness failure into a repair action.  The existing guarded workflow and orphan
    repair paths remain the only authors (NFR-8, ADR-007/008). Its keyset pages and Temporal
    describes are intentionally a fuzzy, read-only observation while workflows keep progressing;
    one clean result is not a lock or a repair authorization.
    """

    def __init__(
        self,
        repository: LifecycleInvariantRepository,
        liveness: WorkflowLivenessInspector,
    ) -> None:
        self._repository = repository
        self._liveness = liveness

    async def scan_once(self, *, batch_size: int = 500) -> LifecycleInvariantReport:
        """Scan all current nonterminal workflow IDs and return PII-free aggregate evidence."""
        if not 1 <= batch_size <= _MAX_WORKFLOW_BATCH_SIZE:
            raise ValueError(
                f"lifecycle invariant batch_size must be between 1 and {_MAX_WORKFLOW_BATCH_SIZE}"
            )
        database = await self._repository.database_snapshot()
        cursor: str | None = None
        scanned = 0
        open_workflows = 0
        closed_workflows = 0
        uninspectable_workflows = 0

        while True:
            workflow_ids = await self._repository.nonterminal_workflow_ids(
                after_workflow_id=cursor,
                limit=batch_size,
            )
            if not workflow_ids:
                break
            _validate_batch(workflow_ids, cursor)
            for workflow_id in workflow_ids:
                scanned += 1
                try:
                    is_open = await self._liveness.is_open(workflow_id)
                except Exception:
                    # The liveness port promises a false result only for definitive closure. A
                    # Temporal transport failure is observable uncertainty, never permission to
                    # call a repair path or report this lifecycle as safely open (ADR-007).
                    uninspectable_workflows += 1
                    continue
                if is_open:
                    open_workflows += 1
                else:
                    closed_workflows += 1
            cursor = workflow_ids[-1]
            if len(workflow_ids) < batch_size:
                break

        return LifecycleInvariantReport(
            database=database,
            scanned_nonterminal_workflows=scanned,
            open_nonterminal_workflows=open_workflows,
            closed_nonterminal_workflows=closed_workflows,
            uninspectable_nonterminal_workflows=uninspectable_workflows,
        )


def _validate_batch(workflow_ids: tuple[str, ...], cursor: str | None) -> None:
    """Reject an unordered/duplicate repository batch before a scanner can loop forever."""
    previous = cursor
    for workflow_id in workflow_ids:
        if not workflow_id or (previous is not None and workflow_id <= previous):
            raise RuntimeError("lifecycle invariant workflow batch must be strictly ordered")
        previous = workflow_id
