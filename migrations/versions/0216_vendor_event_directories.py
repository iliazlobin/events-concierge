"""Prepare vendor directories and public host calendars without activating collection.

Revision ID: 0216
Revises: 0215

Sources remain disabled and unreviewed until an owner admits their exact profiles. Existing
source policy, cadence and public-only Luma visibility are unchanged. The directory adapter reads
LangChain's linked Luma/Goldcast registrations; external conference sites are outside its scope.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0216"
down_revision: str | None = "0215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_MODES = (
    "public_jsonld",
    "livewhale_json",
    "sf_gov_json",
    "datasf_our415",
    "bibliocommons_rss",
    "san_jose_legistar",
    "sunnyvale_legistar",
    "alameda_legistar",
    "oakland_legistar",
    "communico_json",
    "tribe_events_json",
    "localist_json",
    "libcal_ics",
    "civic_engage_rss",
    "midpen_html",
    "usfca_html",
    "calperformances_json",
    "berkeley_rep_html",
    "ybca_html",
    "oakland_html",
    "luma_calendar_json",
    "luma_discover_json",
    "meetup_city_jsonld",
    "meetup_group_ics",
    "tech_week_mcp",
)
_CALENDARS = (
    ("luma-langchain", "LangChain Events on Luma", "cal-mvNH1VHlaFtSMFx"),
    ("luma-semianalysis", "SemiAnalysis", "cal-Y67cYVfrKY12JLQ"),
    ("luma-claude-startups", "Claude Startups", "cal-lRtuNbDBacd0u5K"),
    ("luma-tavily", "Tavily Events", "cal-2mLDnq80EKWoGy8"),
)


def _constraint(modes: tuple[str, ...]) -> None:
    values = ", ".join("'" + mode + "'" for mode in modes)
    op.execute("ALTER TABLE public.catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        "ALTER TABLE public.catalog_sources ADD CONSTRAINT ck_catalog_sources_mode "
        f"CHECK (mode IN ({values}))"
    )


def upgrade() -> None:
    _constraint((*_BASE_MODES, "langchain_html"))
    op.execute("""
        INSERT INTO public.catalog_sources(
          source_key,display_name,publisher,seed_url,approved_origins,region,mode,
          handoff_only,enabled,reviewed_at,review_expires_at,refresh_interval_minutes,
          min_interval_ms,page_limit,collection_horizon_days
        ) VALUES (
          'langchain-events','LangChain Events','LangChain','https://www.langchain.com/events',
          ARRAY['https://www.langchain.com','https://events.langchain.com','https://luma.com'],
          'global','langchain_html',true,false,NULL,NULL,1440,1500,1,90
        )
    """)
    for source_key, display_name, calendar_id in _CALENDARS:
        op.get_bind().execute(
            text("""
            INSERT INTO public.catalog_sources(
              source_key,display_name,publisher,seed_url,approved_origins,region,mode,
              handoff_only,enabled,reviewed_at,review_expires_at,refresh_interval_minutes,
              min_interval_ms,page_limit,collection_horizon_days
            ) VALUES (
              :key,:name,'Luma Calendar',:seed,
              ARRAY['https://api.luma.com','https://api2.luma.com'],
              'global','luma_calendar_json',true,false,NULL,NULL,360,1500,30,90
            )
        """),
            {
                "key": source_key,
                "name": display_name,
                "seed": "https://api.luma.com/calendar/get-items?calendar_api_id="
                + calendar_id
                + "&pagination_limit=20&period=future",
            },
        )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(text("SELECT count(*) FROM public.catalog_sources WHERE mode='langchain_html'"))
        .scalar_one()
    )
    if retained:
        raise RuntimeError("retain vendor source history; use compatible code rollback")
    _constraint(_BASE_MODES)
