"""Social profile capture: URL validation, the Luma projection, and website rel=me discovery.

The binding rule under test throughout is that a social profile belongs to the *enrichment* plane
(``catalog_entity_external_sources``) and never to the FR-19.13 event path
(``canonical_events.entity_profiles``), which admits only a validated LinkedIn URL for a person and
a LinkedIn ``/company/`` URL or an explicit HTTPS website for an organization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from events_concierge.adapters.entity_intelligence.public_sources import (
    OfficialWebsiteSource,
    PublicSourceError,
    SafePublicHttpClient,
    social_profile_source,
)
from events_concierge.adapters.luma_common import (
    luma_public_entity_profiles,
    luma_public_entity_social_links,
)
from events_concierge.adapters.postgres import catalog_refresh_commit as commit_module
from events_concierge.adapters.postgres.catalog_entity_social_links import (
    PostgresCatalogEntitySocialLinks,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.domain.enums import Source
from events_concierge.domain.events import CandidateEvent, EventEntitySocialLink
from events_concierge.domain.social_profiles import (
    SOCIAL_PROVIDER_KEYS,
    social_profile_from_handle,
    social_profile_from_published_value,
    social_profile_from_url,
)

_MIGRATION = Path("migrations/versions/0178_catalog_entity_social_profiles.py")


async def _public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


# --------------------------------------------------------------------------------------------
# URL construction from the bare handles Luma publishes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_key", "handle", "url"),
    [
        ("x_profile", "jonbng", "https://x.com/jonbng"),
        ("x_profile", "@JonBng", "https://x.com/jonbng"),
        ("instagram_profile", "_marcosvalera", "https://www.instagram.com/_marcosvalera"),
        (
            "instagram_profile",
            "jonathan.bangert",
            "https://www.instagram.com/jonathan.bangert",
        ),
        ("tiktok_profile", "_marcosvalera", "https://www.tiktok.com/@_marcosvalera"),
        ("youtube_profile", "marcosvalera", "https://www.youtube.com/@marcosvalera"),
        (
            "youtube_profile",
            "UCabcdefghijklmnopqrstuv",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        ),
    ],
)
def test_bare_handle_becomes_one_canonical_url_per_platform(
    provider_key: str,
    handle: str,
    url: str,
) -> None:
    profile = social_profile_from_handle(provider_key, handle)

    assert profile is not None
    assert profile.provider_key == provider_key
    assert profile.url == url


@pytest.mark.parametrize(
    ("provider_key", "handle"),
    [
        ("x_profile", "home"),
        ("x_profile", "search"),
        ("x_profile", "i"),
        ("x_profile", "intent"),
        ("x_profile", "share"),
        ("x_profile", "login"),
        ("x_profile", "na"),
        ("instagram_profile", "explore"),
        ("instagram_profile", "about"),
        ("youtube_profile", "watch"),
        ("tiktok_profile", "foryou"),
        ("x_profile", "sixteencharacter"),
        ("x_profile", "has space"),
        ("x_profile", "has/slash"),
        ("x_profile", "with?query=1"),
        ("instagram_profile", "trailing."),
        ("instagram_profile", "double..dot"),
        ("youtube_profile", "ab"),
        ("x_profile", ""),
    ],
)
def test_reserved_malformed_and_oversized_handles_are_dropped(
    provider_key: str,
    handle: str,
) -> None:
    assert social_profile_from_handle(provider_key, handle) is None


def test_every_platform_has_its_own_provider_key() -> None:
    assert set(SOCIAL_PROVIDER_KEYS) == {
        "x_profile",
        "instagram_profile",
        "tiktok_profile",
        "youtube_profile",
    }


# --------------------------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "provider_key", "url"),
    [
        ("https://twitter.com/jonbng", "x_profile", "https://x.com/jonbng"),
        ("https://www.twitter.com/JonBng", "x_profile", "https://x.com/jonbng"),
        ("https://x.com/jonbng/", "x_profile", "https://x.com/jonbng"),
        (
            "https://instagram.com/_marcosvalera",
            "instagram_profile",
            "https://www.instagram.com/_marcosvalera",
        ),
        (
            "https://www.tiktok.com/@_marcosvalera",
            "tiktok_profile",
            "https://www.tiktok.com/@_marcosvalera",
        ),
        (
            "https://m.youtube.com/@marcosvalera",
            "youtube_profile",
            "https://www.youtube.com/@marcosvalera",
        ),
        (
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            "youtube_profile",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        ),
        (
            "https://youtube.com/c/SomeChannel",
            "youtube_profile",
            "https://www.youtube.com/c/somechannel",
        ),
    ],
)
def test_canonical_profile_urls_are_accepted_and_twitter_normalises_to_x(
    value: str,
    provider_key: str,
    url: str,
) -> None:
    profile = social_profile_from_url(value)

    assert profile is not None
    assert (profile.provider_key, profile.url) == (provider_key, url)


@pytest.mark.parametrize(
    "value",
    [
        # Search URLs, in every shape a page might publish one.
        "https://x.com/search?q=marcos",
        "https://x.com/search",
        "https://www.instagram.com/explore/tags/ai/",
        "https://www.youtube.com/results?search_query=ai",
        "https://www.tiktok.com/search?q=ai",
        # Reserved and system routes.
        "https://x.com/home",
        "https://x.com/i/flow/login",
        "https://www.youtube.com/watch",
        # Query strings and fragments on an otherwise valid profile.
        "https://x.com/jonbng?utm_source=site",
        "https://x.com/jonbng#pinned",
        # Paths beyond the profile segment.
        "https://x.com/jonbng/status/1234567890",
        "https://www.instagram.com/marcos/reels/",
        "https://www.youtube.com/@marcos/videos",
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/about",
        # Wrong host, wrong scheme, wrong shape.
        "https://x.co/jonbng",
        "https://www.linkedin.com/in/marijan-cipcic-9bab3049",
        "https://twitter.com.evil.example/jonbng",
        "http://x.com/jonbng",
        "https://www.tiktok.com/_marcosvalera",
        "https://user:pass@x.com/jonbng",
        "not a url",
        "",
    ],
)
def test_search_urls_reserved_routes_and_wrong_hosts_are_rejected(value: str) -> None:
    assert social_profile_from_url(value) is None


def test_published_value_reads_either_a_handle_or_a_full_url() -> None:
    assert social_profile_from_published_value("x_profile", "jonbng") is not None
    assert social_profile_from_published_value("x_profile", "https://twitter.com/jonbng") is not None
    # A URL for a different platform than the field it arrived in is not silently re-keyed.
    assert (
        social_profile_from_published_value("x_profile", "https://www.instagram.com/jonbng") is None
    )
    assert social_profile_from_published_value("x_profile", None) is None


# --------------------------------------------------------------------------------------------
# The Luma projection, against a record shaped like the live payload
# --------------------------------------------------------------------------------------------


def _live_shaped_records() -> dict[str, object]:
    """Host and calendar records shaped like the live ``__NEXT_DATA__`` payload."""
    return {
        "calendar_value": {
            "api_id": "cal-example",
            "name": "Daytona",
            "is_personal": False,
            "twitter_handle": "CipcicMarijan",
            "linkedin_handle": "/company/daytona",
        },
        "hosts_value": [
            {
                "name": "Marcos Valera",
                "is_personal": True,
                "instagram_handle": "_marcosvalera",
                "twitter_handle": "_marcosvalera",
                "tiktok_handle": "_marcosvalera",
                "youtube_handle": "marcosvalera",
            },
            {
                "name": "Jonathan Bangert",
                "is_personal": True,
                "instagram_handle": "jonathan.bangert",
                "twitter_handle": "jonbng",
            },
        ],
        "sessions_value": None,
    }


def test_luma_projection_reads_all_four_social_handles_from_a_live_shaped_record() -> None:
    records = _live_shaped_records()

    links = luma_public_entity_social_links(
        calendar_value=records["calendar_value"],
        hosts_value=records["hosts_value"],
        sessions_value=records["sessions_value"],
        organizer_name="Daytona",
        host_names=("Marcos Valera", "Jonathan Bangert"),
        speaker_names=(),
    )

    captured = {(link.name, link.provider_key, link.profile_url) for link in links}
    assert captured == {
        ("Daytona", "x_profile", "https://x.com/cipcicmarijan"),
        ("Marcos Valera", "instagram_profile", "https://www.instagram.com/_marcosvalera"),
        ("Marcos Valera", "x_profile", "https://x.com/_marcosvalera"),
        ("Marcos Valera", "tiktok_profile", "https://www.tiktok.com/@_marcosvalera"),
        ("Marcos Valera", "youtube_profile", "https://www.youtube.com/@marcosvalera"),
        ("Jonathan Bangert", "instagram_profile", "https://www.instagram.com/jonathan.bangert"),
        ("Jonathan Bangert", "x_profile", "https://x.com/jonbng"),
    }


def test_social_handles_never_reach_the_fr_19_13_entity_profile_path() -> None:
    records = _live_shaped_records()

    profiles = luma_public_entity_profiles(
        calendar_value=records["calendar_value"],
        hosts_value=records["hosts_value"],
        sessions_value=records["sessions_value"],
        organizer_name="Daytona",
        host_names=("Marcos Valera", "Jonathan Bangert"),
        speaker_names=(),
    )

    assert [profile.profile_url for profile in profiles] == [
        "https://www.linkedin.com/company/daytona"
    ]
    assert all(
        "x.com" not in profile.profile_url
        and "instagram.com" not in profile.profile_url
        and "tiktok.com" not in profile.profile_url
        and "youtube.com" not in profile.profile_url
        for profile in profiles
    )


def test_featured_guests_and_undisplayed_names_contribute_no_social_links() -> None:
    links = luma_public_entity_social_links(
        calendar_value=None,
        # The caller never passes featured_guests; a record that is passed but whose name is not
        # displayed in its role is still skipped.
        hosts_value=[
            {"name": "Attending Guest", "is_personal": True, "twitter_handle": "guesthandle"},
            {"name": "Marcos Valera", "is_personal": True, "twitter_handle": "_marcosvalera"},
        ],
        sessions_value=None,
        organizer_name=None,
        host_names=("Marcos Valera",),
        speaker_names=(),
    )

    assert [(link.name, link.profile_url) for link in links] == [
        ("Marcos Valera", "https://x.com/_marcosvalera")
    ]


def test_invalid_handles_are_dropped_silently_rather_than_stored_raw() -> None:
    links = luma_public_entity_social_links(
        calendar_value=None,
        hosts_value=[
            {
                "name": "Marcos Valera",
                "is_personal": True,
                "twitter_handle": "home",
                "instagram_handle": "https://www.instagram.com/explore/tags/ai/",
                "tiktok_handle": "  ",
                "youtube_handle": "ab",
            }
        ],
        sessions_value=None,
        organizer_name=None,
        host_names=("Marcos Valera",),
        speaker_names=(),
    )

    assert links == ()


def test_candidate_event_carries_social_links_beside_its_profiles() -> None:
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="https://luma.com/gtm-breakfast-sf",
        title="GTM Breakfast",
        start_at=datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
        registration_url="https://luma.com/gtm-breakfast-sf",
        host_names=("Marcos Valera",),
        entity_social_links=(
            EventEntitySocialLink(
                name="Marcos Valera",
                role="host",
                provider_key="x_profile",
                profile_url="https://twitter.com/_marcosvalera",
            ),
        ),
    )

    assert candidate.entity_profiles == ()
    assert candidate.entity_social_links[0].profile_url == "https://x.com/_marcosvalera"


def test_a_social_link_for_a_name_no_role_displays_is_rejected() -> None:
    with pytest.raises(ValueError, match="not attached to a displayed role"):
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id="https://luma.com/gtm-breakfast-sf",
            title="GTM Breakfast",
            start_at=datetime(2026, 9, 1, 16, 0, tzinfo=UTC),
            registration_url="https://luma.com/gtm-breakfast-sf",
            host_names=("Marcos Valera",),
            entity_social_links=(
                EventEntitySocialLink(
                    name="Someone Else",
                    role="host",
                    provider_key="x_profile",
                    profile_url="https://x.com/someoneelse",
                ),
            ),
        )


# --------------------------------------------------------------------------------------------
# Website discovery -- the entity's own page, never a platform
# --------------------------------------------------------------------------------------------


async def test_official_website_extracts_rel_me_and_anchor_social_links() -> None:
    html = """
    <html><head>
      <title>Example Labs</title>
      <link rel="canonical" href="https://example.com/">
      <link rel="me" href="https://twitter.com/examplelabs">
    </head><body>
      <a rel="me noopener" href="https://www.instagram.com/example.labs">Instagram</a>
      <a rel="menu" href="https://x.com/search?q=example">Search us</a>
      <a href="https://www.tiktok.com/@examplelabs">TikTok</a>
      <a href="https://www.youtube.com/@examplelabs">YouTube</a>
      <a href="https://x.com/home">Home</a>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=html,
            request=request,
        )

    http = SafePublicHttpClient(
        user_agent="events-concierge-test/1.0",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    collected = await OfficialWebsiteSource(http).collect("https://example.com/")

    discovered = {
        (profile.provider_key, profile.url) for profile in collected.discovered_profiles
    }
    assert ("x_profile", "https://x.com/examplelabs") in discovered
    assert ("instagram_profile", "https://www.instagram.com/example.labs") in discovered
    assert ("tiktok_profile", "https://www.tiktok.com/@examplelabs") in discovered
    assert ("youtube_profile", "https://www.youtube.com/@examplelabs") in discovered
    # rel="menu" is not rel="me", and neither the search URL nor the reserved route survives.
    assert all("search" not in url and url != "https://x.com/home" for _, url in discovered)


def test_social_profile_source_records_a_link_without_facts_from_the_platform() -> None:
    collected = social_profile_source("https://twitter.com/examplelabs")

    assert collected.provider_key == "x_profile"
    assert collected.source_url == "https://x.com/examplelabs"
    assert collected.display_name == "X"
    assert [fact.fact_key for fact in collected.facts] == ["profile"]
    assert collected.discovered_profiles == ()


def test_social_profile_source_refuses_a_non_social_url() -> None:
    with pytest.raises(PublicSourceError):
        social_profile_source("https://www.linkedin.com/in/marijan-cipcic-9bab3049")


# --------------------------------------------------------------------------------------------
# Migration contract
# --------------------------------------------------------------------------------------------


def test_migration_widens_only_the_enrichment_plane_vocabulary() -> None:
    sql = _MIGRATION.read_text()

    for provider_key in ("x_profile", "instagram_profile", "tiktok_profile", "youtube_profile"):
        assert f"'{provider_key}'" in sql
    for provider_key in (
        "official_website",
        "github_public",
        "wikidata_public",
        "linkedin_profile",
    ):
        assert f"'{provider_key}'" in sql
    # The FR-19.13 event path is untouched.
    assert "fn_event_entity_profiles_valid" not in sql.replace("``fn_event_entity_profiles_valid``", "")
    assert "canonical_events" not in sql.split('"""', 2)[2]


def test_migration_validators_follow_the_house_security_and_quoting_style() -> None:
    sql = _MIGRATION.read_text()

    assert "CREATE FUNCTION public.fn_normalize_social_profile_url_v1(p_url text)" in sql
    assert "CREATE FUNCTION public.fn_catalog_social_handle_reserved_v1(p_handle text)" in sql
    assert "CREATE FUNCTION public.fn_record_catalog_entity_social_source_v1(" in sql
    assert sql.count("SET search_path = pg_catalog, public") >= 4
    assert "SECURITY DEFINER" in sql
    assert 'op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")' in sql
    assert 'op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")' in sql
    # Every SQL literal is a raw string: a non-raw one corrupts the \\x escapes in the writer guard.
    assert 'op.execute(\n        """' not in sql
    # twitter.com and x.com are one platform.
    assert "'twitter.com', 'www.twitter.com'" in sql
    assert "'https://x.com' || parts.path" in sql


# --------------------------------------------------------------------------------------------
# The commit path that actually writes a link
# --------------------------------------------------------------------------------------------


class _RecordResult:
    def __init__(self, recorded: bool) -> None:
        self._recorded = recorded

    def first(self) -> object:
        return SimpleNamespace(recorded=self._recorded)

    def scalar_one(self) -> bool:
        return True


class _CommitSession:
    """Every statement the publication transaction issues, in order, without a database."""

    def __init__(self, *, recorded: bool = True) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []
        self._recorded = recorded

    async def execute(
        self, statement: object, parameters: dict[str, object] | None = None
    ) -> _RecordResult:
        self.statements.append((str(statement), dict(parameters or {})))
        return _RecordResult(self._recorded)


class _CommitSessionScope:
    def __init__(self, session: _CommitSession) -> None:
        self._session = session

    async def __aenter__(self) -> _CommitSession:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _social_link(name: str, provider_key: str, profile_url: str) -> EventEntitySocialLink:
    return EventEntitySocialLink(
        name=name, role="host", provider_key=provider_key, profile_url=profile_url
    )


def _candidate(source_event_id: str, *links: EventEntitySocialLink) -> CandidateEvent:
    return CandidateEvent(
        source=Source.LUMA,
        source_event_id=source_event_id,
        title="GTM Breakfast SF",
        start_at=datetime(2026, 9, 4, 16, tzinfo=UTC),
        registration_url=f"https://luma.com/{source_event_id}",
        host_names=tuple({link.name for link in links}),
        entity_social_links=links,
    )


async def test_the_writer_issues_one_record_call_per_link() -> None:
    session = _CommitSession()

    recorded = await PostgresCatalogEntitySocialLinks().record_in_session(
        session,  # type: ignore[arg-type]
        source_key="luma_newyork",
        source_event_id="evt-gtm-breakfast-sf",
        links=(
            _social_link("Marcos Valera", "x_profile", "https://x.com/_marcosvalera"),
            _social_link(
                "Marcos Valera", "instagram_profile", "https://www.instagram.com/_marcosvalera"
            ),
        ),
    )

    assert recorded == 2
    assert all(
        "fn_record_catalog_entity_social_source_v1" in statement
        for statement, _ in session.statements
    )
    assert [parameters["social_url"] for _, parameters in session.statements] == [
        "https://x.com/_marcosvalera",
        "https://www.instagram.com/_marcosvalera",
    ]
    for _, parameters in session.statements:
        assert parameters["source_key"] == "luma_newyork"
        assert parameters["source_event_id"] == "evt-gtm-breakfast-sf"
        assert parameters["role"] == "host"
        assert parameters["observed_name"] == "Marcos Valera"
        assert parameters["next_refresh_at"] is not None


async def test_an_unresolved_mention_records_nothing_rather_than_minting_an_entity() -> None:
    session = _CommitSession(recorded=False)

    recorded = await PostgresCatalogEntitySocialLinks().record_in_session(
        session,  # type: ignore[arg-type]
        source_key="luma_newyork",
        source_event_id="evt-1",
        links=(_social_link("Daytona", "x_profile", "https://x.com/cipcicmarijan"),),
    )

    assert recorded == 0
    assert len(session.statements) == 1


async def test_the_commit_path_records_social_links_after_the_entity_index_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring the capture is worthless without.

    Reviewed defect: ``PostgresCatalogEntitySocialLinks`` existed and nothing constructed it, so
    every handle Luma publishes was parsed and then discarded one layer later.  This drives the real
    committer and asserts the record call happens, once per candidate that carries links, and only
    after ``fn_refresh_catalog_entity_index_v3`` has minted the mention the link hangs on.
    """
    session = _CommitSession()
    monkeypatch.setattr(
        commit_module, "system_session_scope", lambda: _CommitSessionScope(session)
    )

    candidates = [
        _candidate(
            "evt-with-links",
            _social_link("Marcos Valera", "x_profile", "https://x.com/_marcosvalera"),
        ),
        _candidate("evt-without-links"),
    ]

    class _Catalog:
        async def embed_candidates(self, _candidates: list[CandidateEvent]) -> list[None]:
            return [None for _ in _candidates]

        async def upsert_candidates_in_session(
            self, _session: object, items: list[CandidateEvent], _vectors: list[None]
        ) -> list[object]:
            return [SimpleNamespace(canonical_event_id=uuid4()) for _ in items]

    class _Observations:
        async def record_in_session(
            self, _session: object, _observations: list[object], _run_key: str
        ) -> None:
            return None

    committer = PostgresCatalogRefreshCommitter(
        _Catalog(),  # type: ignore[arg-type]
        _Observations(),  # type: ignore[arg-type]
        PostgresCatalogEntitySocialLinks(),
    )

    committed = await committer.commit_refresh(
        "luma_bayarea",
        "run-1",
        lease_token=uuid4(),
        candidates=candidates,
    )

    assert committed is not None
    issued = [statement for statement, _ in session.statements]
    social = [index for index, sql in enumerate(issued) if "social_source_v1" in sql]
    index_rebuild = next(
        index for index, sql in enumerate(issued) if "fn_refresh_catalog_entity_index_v3" in sql
    )
    assert len(social) == 1, "one call, for the one candidate that carries a link"
    assert social[0] > index_rebuild, "a link can only hang on a mention that already exists"
    recorded = session.statements[social[0]][1]
    assert recorded["source_event_id"] == "evt-with-links"
    assert recorded["social_url"] == "https://x.com/_marcosvalera"


def test_composition_constructs_the_social_link_writer() -> None:
    # An unwired writer is dead code, which is exactly the state this feature shipped in once.
    source = Path("src/events_concierge/composition.py").read_text()
    assert "PostgresCatalogEntitySocialLinks()" in source


def test_migration_covers_the_mention_lookup_it_documents_as_a_single_row_hit() -> None:
    sql = _MIGRATION.read_text()

    assert "ix_catalog_entity_mentions_social_lookup" in sql
    assert "source_key, source_event_id, role, lower(btrim(observed_name))" in sql
    assert "DROP INDEX IF EXISTS" in sql


def test_migration_and_python_mint_one_external_id_spelling() -> None:
    # Both write paths upsert on (entity_id, provider_key) with DO UPDATE SET external_id, so a
    # second spelling would make the column flip between refreshes.
    sql = _MIGRATION.read_text()
    assert "replace(v_provider_key, '_profile', '')" in sql
    assert "v_provider_key || ':' || v_url" not in sql

    profile = social_profile_from_url("https://x.com/jonbng")
    assert profile is not None
    assert profile.external_id == "x:jonbng"


def test_an_at_handle_is_never_rewritten_into_a_youtube_channel_url() -> None:
    # youtube.com/@UCxxx and youtube.com/channel/UCxxx are different pages.  Stripping the '@'
    # before testing the channel-id shape repaired the first into the second -- a URL turned into
    # something else that merely also resolves, which is the one thing this module must not do.
    channel_id = "UCsBjURrPoezykLs9EqgamOA"

    from_handle = social_profile_from_handle("youtube_profile", f"@{channel_id}")
    assert from_handle is not None
    assert from_handle.url == f"https://www.youtube.com/@{channel_id.lower()}"

    from_url = social_profile_from_url(f"https://www.youtube.com/@{channel_id}")
    assert from_url is not None
    assert from_url.url == f"https://www.youtube.com/@{channel_id.lower()}"

    # The channel page is still reachable by its own two spellings.
    bare = social_profile_from_handle("youtube_profile", channel_id)
    assert bare is not None
    assert bare.url == f"https://www.youtube.com/channel/{channel_id}"
    path = social_profile_from_url(f"https://www.youtube.com/channel/{channel_id}")
    assert path is not None
    assert path.url == f"https://www.youtube.com/channel/{channel_id}"
