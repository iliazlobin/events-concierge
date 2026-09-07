"""Add reviewed anonymous Meetup city JSON-LD sources.

Revision ID: 0126
Revises: 0125
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0126"
down_revision: str | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = (
    "'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415', "
    "'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar', "
    "'alameda_legistar', 'oakland_legistar', 'communico_json', 'tribe_events_json', "
    "'localist_json', 'libcal_ics', 'civic_engage_rss', 'midpen_html', 'usfca_html', "
    "'calperformances_json', 'berkeley_rep_html', 'ybca_html', 'oakland_html', "
    "'luma_calendar_json', 'luma_discover_json'"
)


def _replace_mode_constraint(*, include_meetup_city: bool) -> None:
    values = f"{_MODES}, 'meetup_city_jsonld'" if include_meetup_city else _MODES
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        f"""
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ({values}))
        """
    )


def upgrade() -> None:
    """Admit the closed mode and seed the two tested, reviewed public city pages."""
    _replace_mode_constraint(include_meetup_city=True)
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit, source_revision)
        VALUES
            ('meetup-sf', 'Meetup San Francisco', 'Meetup City',
             'https://www.meetup.com/find/us--ca--san-francisco/',
             ARRAY['https://www.meetup.com'], 'bay_area_9_county',
             'meetup_city_jsonld', true, true, now(), NULL, 120, 1500, 1, 1),
            ('meetup-nyc', 'Meetup New York', 'Meetup City',
             'https://www.meetup.com/find/us--ny--new-york/',
             ARRAY['https://www.meetup.com'], 'new_york_metro',
             'meetup_city_jsonld', true, true, now(), NULL, 120, 1500, 1, 1)
        ON CONFLICT (source_key) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            publisher = EXCLUDED.publisher,
            seed_url = EXCLUDED.seed_url,
            approved_origins = EXCLUDED.approved_origins,
            region = EXCLUDED.region,
            mode = EXCLUDED.mode,
            handoff_only = EXCLUDED.handoff_only,
            enabled = EXCLUDED.enabled,
            reviewed_at = EXCLUDED.reviewed_at,
            review_expires_at = EXCLUDED.review_expires_at,
            refresh_interval_minutes = EXCLUDED.refresh_interval_minutes,
            min_interval_ms = EXCLUDED.min_interval_ms,
            page_limit = EXCLUDED.page_limit,
            source_revision = EXCLUDED.source_revision,
            updated_at = now()
        """
    )


def downgrade() -> None:
    """Retain any collected evidence behind disabled generic public-page records."""
    op.execute(
        """
        UPDATE catalog_sources
        SET mode = 'public_jsonld',
            enabled = false,
            updated_at = now()
        WHERE source_key IN ('meetup-sf', 'meetup-nyc')
        """
    )
    _replace_mode_constraint(include_meetup_city=False)
