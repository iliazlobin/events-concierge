"""Add the reviewed public Luma San Francisco Discover cursor.

Revision ID: 0117
Revises: 0116
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0117"
down_revision: str | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODES = (
    "'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415', "
    "'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar', "
    "'alameda_legistar', 'oakland_legistar', 'communico_json', 'tribe_events_json', "
    "'localist_json', 'libcal_ics', 'civic_engage_rss', 'midpen_html', 'usfca_html', "
    "'calperformances_json', 'berkeley_rep_html', 'ybca_html', 'oakland_html', "
    "'luma_calendar_json'"
)


def _replace_mode_constraint(*, include_discover: bool) -> None:
    values = f"{_MODES}, 'luma_discover_json'" if include_discover else _MODES
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        f"""
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ({values}))
        """
    )


def upgrade() -> None:
    """Replace the narrow GenAI source with Luma's reviewed public Bay Area Discover cursor."""
    _replace_mode_constraint(include_discover=True)
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit, source_revision)
        VALUES
            ('luma-sf', 'Luma San Francisco', 'Luma Discover',
             'https://api.luma.com/discover/get-paginated-events?discover_place_api_id=discplace-BDj7GNbGlsF7Cka&pagination_limit=25',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_discover_json',
             true, true, now(), NULL, 120, 1500, 40, 1)
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
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false,
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
        """
    )


def downgrade() -> None:
    """Retain the SF row as an inactive page seed and restore the GenAI source."""
    op.execute(
        """
        UPDATE catalog_sources
        SET seed_url = 'https://luma.com/sf',
            approved_origins = ARRAY['https://luma.com'],
            mode = 'public_jsonld',
            enabled = false,
            page_limit = 1,
            source_revision = 1,
            updated_at = now()
        WHERE source_key = 'luma-sf'
        """
    )
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = true,
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
        """
    )
    _replace_mode_constraint(include_discover=False)
