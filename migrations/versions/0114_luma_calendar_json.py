"""Upgrade the reviewed Luma calendar from first-page JSON-LD to its bounded cursor.

Revision ID: 0114
Revises: 0113
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0114"
down_revision: str | None = "0113"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = (
    "'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415', "
    "'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar', "
    "'alameda_legistar', 'oakland_legistar', 'communico_json', 'tribe_events_json', "
    "'localist_json', 'libcal_ics', 'civic_engage_rss', 'midpen_html', 'usfca_html', "
    "'calperformances_json', 'berkeley_rep_html', 'ybca_html', 'oakland_html'"
)


def _replace_mode_constraint(*, include_luma: bool) -> None:
    values = f"{_MODES}, 'luma_calendar_json'" if include_luma else _MODES
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        f"""
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ({values}))
        """
    )


def upgrade() -> None:
    """Admit only the approved Generative AI SF public cursor configuration."""
    _replace_mode_constraint(include_luma=True)
    op.execute(
        """
        UPDATE catalog_sources
        SET seed_url = 'https://api.luma.com/calendar/get-items?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future',
            approved_origins = ARRAY['https://api.luma.com'],
            mode = 'luma_calendar_json',
            page_limit = 10,
            source_revision = 2,
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
        """
    )


def downgrade() -> None:
    """Restore the former one-page public JSON-LD source contract."""
    op.execute(
        """
        UPDATE catalog_sources
        SET seed_url = 'https://luma.com/genai-sf',
            approved_origins = ARRAY['https://luma.com'],
            mode = 'public_jsonld',
            page_limit = 1,
            source_revision = 1,
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
        """
    )
    _replace_mode_constraint(include_luma=False)
