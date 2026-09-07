"""Canonical public social profile URLs for the entity *enrichment* plane.

FR-19.13 governs the event response renderer: for producer-verified entity-profile metadata it
admits "only a validated LinkedIn ``/in/`` URL for a person, and a LinkedIn ``/company/`` URL or an
explicit HTTPS website for an organization".  A social handle satisfies none of those shapes, so it
is never admitted into ``canonical_events.entity_profiles`` and nothing in this module widens
``fn_event_entity_profiles_valid``.

Social profiles live in the separate enrichment plane instead -- the
``catalog_entity_external_sources`` table, which already legitimately carries ``official_website``
and ``github_public`` rows for the same entities.  That table is keyed by ``entity_id`` and read
through its own ``SECURITY DEFINER`` functions, so a social link enriches an entity page without
ever entering the event renderer's verified-metadata path.

Two rules bound every value produced here.  Nothing is derived from a display name: a handle only
ever arrives as a structured field a source published in its own record, or as an anchor on a page
the entity itself links.  And nothing is a lookup: a search URL, a query string, or any path beyond
the profile segment is rejected outright rather than trimmed into something that resolves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

SOCIAL_PROVIDER_KEYS: tuple[str, ...] = (
    "x_profile",
    "instagram_profile",
    "tiktok_profile",
    "youtube_profile",
)
SOCIAL_PROVIDER_LABELS: dict[str, str] = {
    "x_profile": "X",
    "instagram_profile": "Instagram",
    "tiktok_profile": "TikTok",
    "youtube_profile": "YouTube",
}

MAX_SOCIAL_PROFILE_URL_LENGTH = 2_048
_MAX_SOCIAL_HANDLE_LENGTH = 120

_X_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_TIKTOK_HOSTS = frozenset({"tiktok.com", "www.tiktok.com"})
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})

_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_INSTAGRAM_HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,29}")
_TIKTOK_HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,23}")
_YOUTUBE_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,29}")
# A channel id is the one YouTube identifier that is case sensitive, and the one bare value that
# must not be read as an ``@`` handle: ``youtube.com/@UCxxx...`` is a different page from
# ``youtube.com/channel/UCxxx...``.  Both forms below are served by YouTube itself; neither is a
# guess about which page a name belongs to.
_YOUTUBE_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_YOUTUBE_LEGACY_NAME = re.compile(r"[A-Za-z0-9_-]{1,60}")

# Reserved and system handles, rejected the way ``fn_catalog_profile_url_placeholder_v1`` rejects a
# placeholder LinkedIn slug: on shape alone, never by comparing the slug to a display name.  The
# list covers the platform routes that would otherwise read as a profile ('home', 'explore',
# 'search', 'i', 'intent', 'share', ...) plus the "no answer" tokens sources emit in a handle field.
#
# Unlike the LinkedIn rule this deliberately ships **no** ``length <= 3`` clause.  That clause is
# calibrated against LinkedIn slugs, where it catches exactly two live rows; three-character handles
# are ordinary on X and Instagram.  A social row is also keyed by ``entity_id`` in the enrichment
# plane rather than folded into ``identity_key``, so a shared handle cannot fuse two entities the
# way a shared LinkedIn slug can -- the fusion risk the length clause exists to cover is absent.
RESERVED_SOCIAL_HANDLES = frozenset(
    {
        "about",
        "account",
        "accounts",
        "admin",
        "api",
        "c",
        "channel",
        "channels",
        "contact",
        "discover",
        "directory",
        "download",
        "embed",
        "example",
        "explore",
        "feed",
        "feeds",
        "following",
        "foryou",
        "help",
        "home",
        "i",
        "intent",
        "legal",
        "live",
        "login",
        "logout",
        "me",
        "messages",
        "music",
        "na",
        "n-a",
        "new",
        "nil",
        "none",
        "notifications",
        "null",
        "p",
        "playlist",
        "policies",
        "privacy",
        "profile",
        "reel",
        "reels",
        "results",
        "search",
        "session",
        "settings",
        "share",
        "shorts",
        "signup",
        "stories",
        "support",
        "tag",
        "terms",
        "test",
        "tos",
        "trending",
        "tv",
        "undefined",
        "unknown",
        "upload",
        "user",
        "username",
        "video",
        "watch",
        "www",
    }
)


@dataclass(frozen=True, slots=True)
class SocialProfile:
    """One canonical social profile URL, ready for the enrichment plane.

    The shape mirrors ``DiscoveredProfile`` in the public-source adapters so a social profile can be
    carried through the same enrichment path as a LinkedIn or GitHub link.
    """

    provider_key: str
    url: str
    external_id: str
    display_name: str


def social_profile_from_handle(  # noqa: PLR0911
    provider_key: str,
    value: str,
) -> SocialProfile | None:
    """Build a canonical URL from a bare handle a source published in a structured field.

    Returns ``None`` for anything that is not a plain handle for that platform.  A rejected value is
    dropped, never stored raw and never repaired into a URL that merely resolves.
    """
    handle = value.strip()
    # Whether the value arrived as an ``@`` handle decides which YouTube page it names, so it is
    # recorded before the prefix is stripped: ``@UCxxx...`` is the handle page, and rewriting it to
    # ``/channel/UCxxx...`` would swap in a different page that merely also resolves.
    at_handle = handle.startswith("@")
    if at_handle:
        handle = handle[1:]
    if (
        not handle
        or len(handle) > _MAX_SOCIAL_HANDLE_LENGTH
        or any(character.isspace() for character in handle)
        or any(character in handle for character in "/\\:?#&=%@")
    ):
        return None
    if (
        provider_key == "youtube_profile"
        and not at_handle
        and _YOUTUBE_CHANNEL_ID.fullmatch(handle) is not None
    ):
        return _profile("youtube_profile", f"channel/{handle}")
    if not _valid_handle(provider_key, handle.casefold()):
        return None
    lowered = handle.casefold()
    if provider_key == "x_profile":
        return _profile("x_profile", lowered)
    if provider_key == "instagram_profile":
        return _profile("instagram_profile", lowered)
    if provider_key == "tiktok_profile":
        return _profile("tiktok_profile", f"@{lowered}")
    if provider_key == "youtube_profile":
        return _profile("youtube_profile", f"@{lowered}")
    return None


def social_profile_from_url(value: str) -> SocialProfile | None:  # noqa: PLR0911
    """Accept only a canonical per-platform profile URL, on its own host, with no query.

    ``twitter.com`` normalises to ``x.com``: one platform, one provider key.  A search URL, a query
    string, a fragment, a port other than 443, or any path beyond the profile segment is rejected.
    """
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_SOCIAL_PROFILE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        return None
    host = parsed.hostname.rstrip(".").casefold()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if host in _X_HOSTS:
        return _single_segment_profile("x_profile", segments)
    if host in _INSTAGRAM_HOSTS:
        return _single_segment_profile("instagram_profile", segments)
    if host in _TIKTOK_HOSTS:
        return _at_segment_profile("tiktok_profile", segments)
    if host in _YOUTUBE_HOSTS:
        return _youtube_profile(segments)
    return None


def social_profile_from_published_value(provider_key: str, value: object) -> SocialProfile | None:
    """Read one published social field that may hold either a bare handle or a full profile URL."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "//" in candidate or "://" in candidate:
        profile = social_profile_from_url(candidate)
        return profile if profile is not None and profile.provider_key == provider_key else None
    return social_profile_from_handle(provider_key, candidate)


def _single_segment_profile(provider_key: str, segments: list[str]) -> SocialProfile | None:
    if len(segments) != 1 or segments[0].startswith("@"):
        return None
    return social_profile_from_handle(provider_key, segments[0])


def _at_segment_profile(provider_key: str, segments: list[str]) -> SocialProfile | None:
    if len(segments) != 1 or not segments[0].startswith("@"):
        return None
    return social_profile_from_handle(provider_key, segments[0])


def _youtube_profile(segments: list[str]) -> SocialProfile | None:
    if len(segments) == 1 and segments[0].startswith("@"):
        return social_profile_from_handle("youtube_profile", segments[0])
    if len(segments) != 2:  # noqa: PLR2004
        return None
    prefix, name = segments
    if prefix == "channel" and _YOUTUBE_CHANNEL_ID.fullmatch(name) is not None:
        return _profile("youtube_profile", f"channel/{name}")
    if (
        prefix in {"c", "user"}
        and _YOUTUBE_LEGACY_NAME.fullmatch(name) is not None
        and name.casefold() not in RESERVED_SOCIAL_HANDLES
    ):
        return _profile("youtube_profile", f"{prefix}/{name.casefold()}")
    return None


def _valid_handle(provider_key: str, handle: str) -> bool:
    if handle in RESERVED_SOCIAL_HANDLES or ".." in handle or handle.endswith("."):
        return False
    pattern = {
        "x_profile": _X_HANDLE,
        "instagram_profile": _INSTAGRAM_HANDLE,
        "tiktok_profile": _TIKTOK_HANDLE,
        "youtube_profile": _YOUTUBE_HANDLE,
    }.get(provider_key)
    return pattern is not None and pattern.fullmatch(handle) is not None


def _profile(provider_key: str, path: str) -> SocialProfile:
    host = {
        "x_profile": "x.com",
        "instagram_profile": "www.instagram.com",
        "tiktok_profile": "www.tiktok.com",
        "youtube_profile": "www.youtube.com",
    }[provider_key]
    slug = provider_key.removesuffix("_profile")
    return SocialProfile(
        provider_key=provider_key,
        url=f"https://{host}/{path}",
        external_id=f"{slug}:{path}",
        display_name=SOCIAL_PROVIDER_LABELS[provider_key],
    )
