"""The mandatory pre-registration conflict gate (FR-4.5): a hard overlap with an opaque busy block
BLOCKS a candidate (it is never attempted); near-adjacent / tentative / transparent only DEMOTE."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .enums import ConflictVerdict

NEAR_ADJACENT = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class BusyBlock:
    start: datetime
    end: datetime
    transparent: bool = False  # "free"/transparent events never hard-block
    tentative: bool = False
    all_day: bool = False

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


def evaluate_conflict(
    candidate_start: datetime,
    candidate_end: datetime,
    busy_blocks: list[BusyBlock],
) -> ConflictVerdict:
    """Return the strongest verdict across all busy blocks. BLOCKED dominates DEMOTE dominates OK."""
    verdict = ConflictVerdict.OK
    for block in busy_blocks:
        if block.overlaps(candidate_start, candidate_end):
            if block.transparent or block.tentative or block.all_day:
                verdict = _raise_to(verdict, ConflictVerdict.DEMOTE)
            else:
                return ConflictVerdict.BLOCKED  # hard overlap -- cannot be raised further
        elif _near_adjacent(block, candidate_start, candidate_end):
            verdict = _raise_to(verdict, ConflictVerdict.DEMOTE)
    return verdict


def _near_adjacent(block: BusyBlock, start: datetime, end: datetime) -> bool:
    gap_before = start - block.end
    gap_after = block.start - end
    return timedelta(0) <= gap_before <= NEAR_ADJACENT or timedelta(0) <= gap_after <= NEAR_ADJACENT


_RANK = {ConflictVerdict.OK: 0, ConflictVerdict.DEMOTE: 1, ConflictVerdict.BLOCKED: 2}


def _raise_to(current: ConflictVerdict, candidate: ConflictVerdict) -> ConflictVerdict:
    return candidate if _RANK[candidate] > _RANK[current] else current
