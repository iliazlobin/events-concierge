"""Public schema.org roles reach company indexing without indexing attendees."""

import json

from events_concierge.adapters.crawl.acl import parse_jsonld


def test_explicit_jsonld_organizers_and_sponsors_keep_their_public_names_and_profiles() -> None:
    event = {
        "@type": "Event",
        "name": "Builder Meetup",
        "startDate": "2026-11-01T12:00:00Z",
        "url": "https://events.example.test/meetup",
        "organizer": [
            {"@type": "Organization", "name": "Example Labs", "url": "https://labs.example.test/"},
            {
                "@type": "Organization",
                "name": "Example Cloud",
                "url": "https://cloud.example.test/",
            },
        ],
        "sponsor": {
            "@type": "Corporation",
            "name": "Example Chips",
            "url": "https://chips.example.test/",
        },
        "attendee": {"@type": "Person", "name": "Private Guest", "email": "private@example.test"},
    }
    [candidate] = parse_jsonld(
        '<script type="application/ld+json">' + json.dumps(event) + "</script>",
        "https://events.example.test/",
        strict=True,
    )
    assert candidate.organizer_name == "Example Labs"
    assert candidate.host_names == ("Example Cloud",)
    assert candidate.partner_names == ("Example Chips",)
    assert [(p.name, p.role, p.kind) for p in candidate.entity_profiles] == [
        ("Example Labs", "organizer", "organization"),
        ("Example Cloud", "host", "organization"),
        ("Example Chips", "partner", "organization"),
    ]
    assert candidate.raw == {}
    assert "Private Guest" not in repr(candidate)
    assert "private@example.test" not in repr(candidate)


def test_invalid_optional_profile_does_not_remove_a_published_role_name() -> None:
    event = {
        "@type": "Event",
        "name": "Builder Meetup",
        "startDate": "2026-11-01T12:00:00Z",
        "organizer": {"@type": "Organization", "name": "Example Labs", "url": "http://127.0.0.1/"},
    }
    [candidate] = parse_jsonld(
        '<script type="application/ld+json">' + json.dumps(event) + "</script>",
        "https://events.example.test/",
        strict=True,
    )
    assert candidate.organizer_name == "Example Labs"
    assert candidate.entity_profiles == ()
