"""Approve the detail origin for every reviewed Luma host calendar.

Revision ID: 0169
Revises: 0168
Create Date: 2026-08-26

A Luma *listing* -- a Discover page or a calendar cursor page -- carries identity, timing, place,
and price and nothing else.  Description, speakers, partners, attendance, and registration status
live only on the per-event record at ``api2.luma.com/event/get``.  The Discover adapter has always
followed that lane; the calendar adapter did not, so the same event arrived rich through
``luma-sf`` and bare through ``luma-thecommons``, and whichever source refreshed last decided what
the reader saw.

The calendar adapter now shares Discover's detail lane, which means a calendar row must approve the
same two origins a Discover row does.  ``_profile_for_source`` requires that pair exactly, so this
migration and the adapter change are one unit: a calendar row left on the single origin refuses to
run rather than silently skipping enrichment.

This raises approved egress, and it is worth being exact about what does and does not bound it.
Each refresh of a calendar now costs one GET per retained future event on top of its pages --
measured live, 723 detail GETs across the 64 reviewed calendars per cycle, against roughly 78
cursor pages before.  What paces those requests is the adapter's own per-host floor, this row's
``min_interval_ms`` (1.5s), applied by ``_wait_for_host_slot`` before every GET.  It is *not* the
Pacer: ``_pacer_request`` admits one token per *refresh*, under ``Source.PUBLIC_JSONLD``, which has
no entry in ``default_source_budgets()`` and so falls to the generic 5/s default -- the Luma-shaped
``PacerBudget`` in ``adapters/policy/pacer.py`` governs the browser RSVP lane, not this one.  On a
360-minute cadence the 64 calendars come to about 134 requests an hour spread across the fleet.

``_lease_seconds_for`` was widened in the same change to reserve for those requests, because the
previous reservation counted pages only and left these sources depending on the 300-second process
default.  That reservation is now the binding constraint on ``page_limit`` for a Luma row: at
1.5s per unit it admits at most 90 pages before the one-hour lease ceiling makes the source fail
closed, which is why the registry test asserts an upper bound as well as a lower one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0169"
down_revision: str | None = "0168"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit the detail origin on every non-retired Luma calendar row."""
    op.execute(
        """
        UPDATE catalog_sources
        SET approved_origins = ARRAY['https://api.luma.com', 'https://api2.luma.com'],
            reviewed_at = now(),
            updated_at = now()
        WHERE mode = 'luma_calendar_json'
          AND retired_at IS NULL
        """
    )


def downgrade() -> None:
    """Return every calendar row to the listing origin alone."""
    op.execute(
        """
        UPDATE catalog_sources
        SET approved_origins = ARRAY['https://api.luma.com'],
            updated_at = now()
        WHERE mode = 'luma_calendar_json'
          AND retired_at IS NULL
        """
    )
