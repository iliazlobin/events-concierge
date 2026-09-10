"""Durable source work and bounded, payload-free command investigation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class CommandInvestigationLeaseLostError(RuntimeError):
    """The exact command attempt no longer owns authority to read or advance its plan."""


@dataclass(frozen=True, slots=True)
class CommandTask:
    position: int
    source_key: str
    run_key: str
    status: str
    attempt_count: int
    available_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_outcome_code: str | None
    candidate_count: int
    canonical_count: int
    last_progress_at: datetime | None = None
    # Database-relative scheduling hint; start_task remains the eligibility authority.
    retry_after_seconds: int | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "already_succeeded", "queued", "skipped", "failed"}
