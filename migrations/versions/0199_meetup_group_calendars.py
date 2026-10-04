"""Admit the reviewed public Meetup group-calendar adapter; activate sources separately.

Revision ID: 0199
Revises: 0195
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0199"
down_revision: str | None = "0195"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_MODES = (
    "public_jsonld", "livewhale_json", "sf_gov_json", "datasf_our415", "bibliocommons_rss",
    "san_jose_legistar", "sunnyvale_legistar", "alameda_legistar", "oakland_legistar",
    "communico_json", "tribe_events_json", "localist_json", "libcal_ics", "civic_engage_rss",
    "midpen_html", "usfca_html", "calperformances_json", "berkeley_rep_html", "ybca_html",
    "oakland_html", "luma_calendar_json", "luma_discover_json", "meetup_city_jsonld",
)


def _constraint(modes: tuple[str, ...]) -> None:
    values = ", ".join("'" + mode + "'" for mode in modes)
    op.execute("ALTER TABLE public.catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute("ALTER TABLE public.catalog_sources ADD CONSTRAINT ck_catalog_sources_mode "
               f"CHECK (mode IN ({values}))")


def upgrade() -> None:
    _constraint((*_BASE_MODES, "meetup_group_ics"))


def downgrade() -> None:
    # Preserve registration/audit/history; an application rollback first disables these
    # sources and retains the compatible schema. Never rewrite or delete reviewed sources.
    count = op.get_bind().execute(text(
        "SELECT count(*) FROM public.catalog_sources WHERE mode='meetup_group_ics'"
    )).scalar_one()
    if count:
        raise RuntimeError("retain Meetup group source history; roll back compatible code without schema downgrade")
    _constraint(_BASE_MODES)
