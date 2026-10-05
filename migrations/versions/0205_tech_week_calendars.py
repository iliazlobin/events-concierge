"""Register disabled official SF/LA 2026 Tech Week sources and bounded edition windows.

Revision ID: 0205
Revises: 0202

0203/0204 belong to independent library-recovery/identity tasks. Owner review/activation is
separate; the migration neither grants policy admission nor initiates collection.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0205"
down_revision: str | None = "0202"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_MODES = (
    "public_jsonld", "livewhale_json", "sf_gov_json", "datasf_our415", "bibliocommons_rss",
    "san_jose_legistar", "sunnyvale_legistar", "alameda_legistar", "oakland_legistar",
    "communico_json", "tribe_events_json", "localist_json", "libcal_ics", "civic_engage_rss",
    "midpen_html", "usfca_html", "calperformances_json", "berkeley_rep_html", "ybca_html",
    "oakland_html", "luma_calendar_json", "luma_discover_json", "meetup_city_jsonld",
    "meetup_group_ics",
)
_PREPARE = "public.fn_prepare_catalog_collection_window_v1(text,text,uuid,integer)"
_WINDOW_VALUES = """VALUES(p_source_key,p_run_key,s.source_revision,s.collection_horizon_days,v_now,
     v_now+s.collection_horizon_days*interval '24 hours') RETURNING * INTO w;"""
_PROFILE = """s.mode='tech_week_mcp' AND s.collection_horizon_days=15
       AND s.approved_origins=ARRAY['https://www.tech-week.com']::text[]
       AND ((s.source_key='tech-week-sf-2026' AND s.seed_url='https://www.tech-week.com/calendar/sf')
         OR (s.source_key='tech-week-la-2026' AND s.seed_url='https://www.tech-week.com/calendar/la'))"""
_EDITION_VALUES = f"""VALUES(p_source_key,p_run_key,s.source_revision,s.collection_horizon_days,
     CASE WHEN {_PROFILE} THEN '2026-10-05T07:00:00Z'::timestamptz ELSE v_now END,
     CASE WHEN {_PROFILE} THEN '2026-10-20T07:00:00Z'::timestamptz
       ELSE v_now+s.collection_horizon_days*interval '24 hours' END) RETURNING * INTO w;"""


def _constraint(modes: tuple[str, ...]) -> None:
    values = ", ".join("'" + mode + "'" for mode in modes)
    op.execute("ALTER TABLE public.catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute("ALTER TABLE public.catalog_sources ADD CONSTRAINT ck_catalog_sources_mode "
               f"CHECK (mode IN ({values}))")


def _edition_window(before: str, after: str) -> None:
    definition = op.get_bind().execute(
        text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": _PREPARE},
    ).scalar_one()
    if definition.count(before) != 1:
        raise RuntimeError("unexpected frozen collection-window contract")
    # CREATE OR REPLACE preserves the existing owner, ACL, SECDEF and lease/revision fences.
    op.execute(definition.replace(before, after))


def upgrade() -> None:
    _constraint((*_BASE_MODES, "tech_week_mcp"))
    _edition_window(_WINDOW_VALUES, _EDITION_VALUES)
    op.execute("""
        INSERT INTO public.catalog_sources(
          source_key,display_name,publisher,seed_url,approved_origins,region,mode,
          handoff_only,enabled,reviewed_at,review_expires_at,refresh_interval_minutes,
          min_interval_ms,page_limit,collection_horizon_days
        ) VALUES
          ('tech-week-sf-2026','SF Tech Week 2026','Tech Week',
           'https://www.tech-week.com/calendar/sf',ARRAY['https://www.tech-week.com'],
           'bay_area_9_county','tech_week_mcp',true,false,NULL,'2026-10-20T07:00:00Z',180,1500,40,15),
          ('tech-week-la-2026','LA Tech Week 2026','Tech Week',
           'https://www.tech-week.com/calendar/la',ARRAY['https://www.tech-week.com'],
           'los_angeles','tech_week_mcp',true,false,NULL,'2026-10-20T07:00:00Z',180,1500,40,15),
          ('luma-sf-tech-week-2026','SF Tech Week 2026 on Luma','Luma Tech Week',
           'https://api.luma.com/calendar/get-items?calendar_api_id=cal-bR2dxhC1V6wCtK8&pagination_limit=20&period=future',
           ARRAY['https://api.luma.com','https://api2.luma.com'],
           'bay_area_9_county','luma_calendar_json',true,false,NULL,'2026-10-20T07:00:00Z',180,1500,30,15),
          ('luma-la-tech-week-2026','LA Tech Week 2026 on Luma','Luma Tech Week',
           'https://api.luma.com/calendar/get-items?calendar_api_id=cal-BVYkgFSqxs8DEOL&pagination_limit=20&period=future',
           ARRAY['https://api.luma.com','https://api2.luma.com'],
           'los_angeles','luma_calendar_json',true,false,NULL,'2026-10-20T07:00:00Z',180,1500,30,15)
    """)


def downgrade() -> None:
    count = op.get_bind().execute(text(
        "SELECT count(*) FROM public.catalog_sources WHERE mode='tech_week_mcp'"
    )).scalar_one()
    if count:
        raise RuntimeError("retain Tech Week source history; roll back compatible code without schema downgrade")
    _edition_window(_EDITION_VALUES, _WINDOW_VALUES)
    _constraint(_BASE_MODES)
