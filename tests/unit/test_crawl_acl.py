"""Unit tests for the free-crawl JSON-LD Anti-Corruption Layer (FR-3.x)."""

from __future__ import annotations

from datetime import UTC

from events_concierge.adapters.crawl.acl import parse_jsonld
from events_concierge.domain.enums import Source

_BASE_URL = "https://example.org/events"


def test_parses_single_event_block() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": "Jazz in the Park",
        "startDate": "2026-08-01T19:00:00-04:00",
        "endDate": "2026-08-01T21:00:00-04:00",
        "url": "https://example.org/events/jazz",
        "description": "An evening of live jazz.",
        "location": {
          "@type": "Place",
          "name": "Central Park",
          "geo": {"@type": "GeoCoordinates", "latitude": 40.785, "longitude": -73.968},
          "address": {"@type": "PostalAddress", "addressLocality": "New York"}
        },
        "offers": {"@type": "Offer", "price": "0", "availability": "https://schema.org/InStock"}
      }
      </script>
    </head><body></body></html>
    """
    events = parse_jsonld(html, _BASE_URL)

    assert len(events) == 1
    event = events[0]
    assert event.source is Source.PUBLIC_JSONLD
    assert event.source_event_id == "https://example.org/events/jazz"
    assert event.registration_url == "https://example.org/events/jazz"
    assert event.title == "Jazz in the Park"
    assert event.start_at.tzinfo is not None
    assert event.start_at.astimezone(UTC).hour == 23
    assert event.end_at is not None
    assert event.venue_name == "Central Park"
    assert event.geo is not None
    assert event.geo.lat == 40.785
    assert event.geo.lon == -73.968
    assert event.city == "New York"
    assert event.description == "An evening of live jazz."
    assert event.is_free is True


def test_parses_graph_variant() -> None:
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "WebSite", "name": "Listings"},
        {
          "@type": ["Event", "MusicEvent"],
          "name": "Open Mic Night",
          "startDate": "2026-09-10T18:30:00"
        }
      ]
    }
    </script>
    """
    events = parse_jsonld(html, _BASE_URL)

    assert len(events) == 1
    event = events[0]
    assert event.title == "Open Mic Night"
    # startDate was naive -> assumed UTC.
    assert event.start_at.tzinfo is not None
    assert event.start_at.utcoffset() == UTC.utcoffset(None)
    # No url -> stable synthetic id + base_url fallback for registration.
    assert event.source_event_id.startswith("jsonld:")
    assert event.registration_url == _BASE_URL
    assert event.is_free is None


def test_parses_upcoming_events_from_schema_item_list_with_price_truth() -> None:
    """A public Luma-style calendar remains discovery-only and retains paid listings (FR-3.1/5.10)."""
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "position": 1,
          "item": {
            "@type": "Event",
            "name": "Free AI Builder Night",
            "startDate": "2026-07-20T18:00:00-07:00",
            "url": "https://luma.com/free-ai-builder-night",
            "location": {
              "@type": "Place",
              "name": "SF Test Venue",
              "address": {"@type": "PostalAddress", "addressLocality": "San Francisco"}
            },
            "offers": {"@type": "Offer", "price": "0"}
          }
        },
        {
          "@type": "ListItem",
          "position": 2,
          "item": {
            "@type": "Event",
            "name": "Paid AI Summit",
            "startDate": "2026-07-21T09:00:00-07:00",
            "url": "https://luma.com/paid-ai-summit",
            "offers": {"@type": "Offer", "price": 25}
          }
        }
      ]
    }
    </script>
    """

    events = parse_jsonld(html, "https://luma.com/genai-sf")

    assert [(event.title, event.is_free) for event in events] == [
        ("Paid AI Summit", False),
        ("Free AI Builder Night", True),
    ]
    free = events[1]
    assert free.source is Source.PUBLIC_JSONLD
    assert free.source_event_id == "https://luma.com/free-ai-builder-night"
    assert free.registration_url == "https://luma.com/free-ai-builder-night"
    assert free.venue_name == "SF Test Venue"
    assert free.city == "San Francisco"


def test_mixed_or_unknown_jsonld_offer_tiers_are_not_verified_free() -> None:
    """Tiered and partially specified offers route through the FR-5.10 handoff guard."""
    html = """
    <script type="application/ld+json">
    [
      {
        "@type": "Event",
        "name": "Mixed tier event",
        "startDate": "2026-07-20T18:00:00-07:00",
        "offers": [{"@type": "Offer", "price": "0"}, {"@type": "Offer", "price": "25"}]
      },
      {
        "@type": "Event",
        "name": "Free plus unspecified tier",
        "startDate": "2026-07-21T18:00:00-07:00",
        "offers": [{"@type": "Offer", "price": "0"}, {"@type": "Offer"}]
      },
      {
        "@type": "Event",
        "name": "All free tiers",
        "startDate": "2026-07-22T18:00:00-07:00",
        "offers": [{"@type": "Offer", "price": "0"}, {"@type": "Offer", "price": "free"}]
      }
    ]
    </script>
    """

    events = {event.title: event for event in parse_jsonld(html, _BASE_URL)}

    assert events["Mixed tier event"].is_free is None
    assert events["Free plus unspecified tier"].is_free is None
    assert events["All free tiers"].is_free is True


def test_reused_calendar_url_gets_a_stable_per_event_source_id() -> None:
    """A calendar homepage reused as JSON-LD URL cannot overwrite a different event's provenance (FR-3.8)."""
    html = """
    <script type="application/ld+json">
    [
      {
        "@type": "Event",
        "name": "Repeated calendar card",
        "startDate": "2026-07-20T18:00:00-07:00",
        "url": "https://events.example.test/calendar",
        "location": {
          "@type": "Place",
          "name": "First room",
          "address": {"@type": "PostalAddress", "addressLocality": "San Francisco"}
        }
      },
      {
        "@type": "Event",
        "name": "Repeated calendar card",
        "startDate": "2026-07-20T18:00:00-07:00",
        "url": "https://events.example.test/calendar",
        "location": {
          "@type": "Place",
          "name": "Second room",
          "address": {"@type": "PostalAddress", "addressLocality": "San Jose"}
        }
      }
    ]
    </script>
    """

    events = parse_jsonld(html, _BASE_URL)

    assert len({event.source_event_id for event in events}) == 2
    assert all(
        event.source_event_id.startswith("https://events.example.test/calendar|jsonld:")
        for event in events
    )


def test_exact_duplicate_jsonld_blocks_collapse_without_claiming_a_price() -> None:
    """A page's repeated structured-data block becomes one safe source observation (FR-3.7/FR-3.8)."""
    html = """
    <script type="application/ld+json">
    [
      {
        "@type": "Event",
        "name": "Duplicated event",
        "startDate": "2026-07-20T18:00:00-07:00",
        "url": "https://events.example.test/duplicated",
        "offers": {"@type": "Offer", "price": "0"}
      },
      {
        "@type": "Event",
        "name": "Duplicated event",
        "startDate": "2026-07-20T18:00:00-07:00",
        "url": "https://events.example.test/duplicated"
      }
    ]
    </script>
    """

    events = parse_jsonld(html, _BASE_URL)

    assert len(events) == 1
    assert events[0].is_free is None


def test_malformed_block_is_skipped_without_raising() -> None:
    html = """
    <script type="application/ld+json">{ this is not valid json </script>
    <script type="application/ld+json">
    {"@type": "Event", "name": "Good One", "startDate": "2026-10-01T12:00:00Z"}
    </script>
    """
    events = parse_jsonld(html, _BASE_URL)

    assert len(events) == 1
    assert events[0].title == "Good One"


def test_non_event_types_are_ignored() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "Organization", "name": "Not An Event"}
    </script>
    """
    assert parse_jsonld(html, _BASE_URL) == []
