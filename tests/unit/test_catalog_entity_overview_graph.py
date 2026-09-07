"""Live-capture coverage for the entities landing graph: top hubs and how they interconnect.

``fn_get_catalog_entity_overview_graph_v1`` returns the *same* jsonb envelope as
``fn_get_catalog_entity_graph_v1``, which is the whole point of it -- one parser, one set of domain
dataclasses and one set of TypeScript types serve both surfaces.  These tests pin the ways the
overview payload is nonetheless a different animal inside that envelope, because each is a place a
capability revision could silently drift:

* ``focus_id`` is the literal ``'overview'`` and not a namespaced node id -- there is no ego;
* ring 0 is *every* ranked hub rather than one subject, and a hub sharing no event with another hub
  is still emitted, drawn isolated;
* ring 1 is one representative event per connected pair, so ``shared_event_count`` on an event node
  is the pair's true total and can be far larger than the single node standing for it -- the
  summary the view is obliged to disclose;
* there are no topic nodes and no exact-name review candidates.

Every payload below is one live capture, whole, taken against the running catalog at small limits
rather than production limits and pasted unedited.  The three were chosen to separate the three
disclosures the envelope makes, which no single frame shows at once:

* ``_TOP_HUBS`` -- 6 hubs, 4 of them paired into 2 bridge events and 2 in no pair at all;
* ``_PAIR_CAPPED`` -- the same 6 hubs with ``p_pair_limit`` at 1, so the pair cap bites and
  ``truncated.events`` flips while ``truncated.peers`` stays where it was;
* ``_PEOPLE_ONLY`` -- ``kind=person``, 12 hubs, where three people meet at *one* event and their
  three pairs therefore elect the *same* representative.  That capture is why ``counts.events``
  (1 node) and ``counts.events_total`` (3 pairs) are different quantities and why
  ``truncated.events`` is read off the pair cap instead of off a comparison between them.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from events_concierge.adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from events_concierge.adapters.postgres.catalog_entity_graph import graph_from_payload

_TOP_HUBS: dict[str, Any] = {
    "edges": [
        {
            "a": "entity:62d0fe66-3fb6-4810-b5b2-764b8dddd6d5",
            "b": "event:07f91654-c873-4f70-9b64-5dbc88ea95b4",
            "kind": "mention",
            "roles": ["organizer"],
            "observed_at": "2026-08-27T00:38:52.697116+00:00",
            "source_labels": ["Accent Is A Superpower (online & offline events)"],
        },
        {
            "a": "entity:74d7e6cc-209d-471f-90f2-00a08151201f",
            "b": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "kind": "mention",
            "roles": ["host"],
            "observed_at": "2026-08-27T00:51:52.088298+00:00",
            "source_labels": ["The Commons"],
        },
        {
            "a": "entity:99e5b580-0449-4958-8945-74eb62484b93",
            "b": "event:07f91654-c873-4f70-9b64-5dbc88ea95b4",
            "kind": "mention",
            "roles": ["host"],
            "observed_at": "2026-08-27T00:38:52.697116+00:00",
            "source_labels": ["Accent Is A Superpower (online & offline events)"],
        },
        {
            "a": "entity:dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "b": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "kind": "mention",
            "roles": ["organizer"],
            "observed_at": "2026-08-27T00:51:52.088298+00:00",
            "source_labels": ["The Commons"],
        },
    ],
    "nodes": [
        {
            "ring": 0,
            "label": "Claude Community Events",
            "roles": ["organizer"],
            "degree": 75,
            "node_id": "entity:2aa228d7-56e2-4e7f-928a-6bb383b7a689",
            "entity_id": "2aa228d7-56e2-4e7f-928a-6bb383b7a689",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://claude.ai",
            "profile_url": "https://claude.ai",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Brooklyn Grange: Sunset Park and Brooklyn Navy Yard",
            "roles": ["organizer"],
            "degree": 34,
            "node_id": "entity:503e5cc5-1dec-4cd1-9350-22d1940150a4",
            "entity_id": "503e5cc5-1dec-4cd1-9350-22d1940150a4",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Accent Is A Superpower (online & offline events)",
            "roles": ["organizer"],
            "degree": 37,
            "node_id": "entity:62d0fe66-3fb6-4810-b5b2-764b8dddd6d5",
            "entity_id": "62d0fe66-3fb6-4810-b5b2-764b8dddd6d5",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://accentaccent.com/",
            "profile_url": "https://accentaccent.com/",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "The SF Commons",
            "roles": ["host"],
            "degree": 88,
            "node_id": "entity:74d7e6cc-209d-471f-90f2-00a08151201f",
            "entity_id": "74d7e6cc-209d-471f-90f2-00a08151201f",
            "node_kind": "entity",
            "entity_kind": "unknown",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Accent Accent",
            "roles": ["host"],
            "degree": 36,
            "node_id": "entity:99e5b580-0449-4958-8945-74eb62484b93",
            "entity_id": "99e5b580-0449-4958-8945-74eb62484b93",
            "node_kind": "entity",
            "entity_kind": "unknown",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "The Commons",
            "roles": ["organizer"],
            "degree": 83,
            "node_id": "entity:dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "entity_id": "dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://thesfcommons.com",
            "profile_url": "https://thesfcommons.com",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "city": None,
            "ring": 1,
            "label": "9 Weeks Poetry and Performance Workshop x Pamela Sneed",
            "degree": 2,
            "end_at": "2027-04-20T04:30:00+00:00",
            "topics": ["arts", "workshop"],
            "is_past": False,
            "node_id": "event:07f91654-c873-4f70-9b64-5dbc88ea95b4",
            "start_at": "2027-02-08T02:00:00+00:00",
            "ego_roles": ["host", "organizer"],
            "node_kind": "event",
            "venue_name": "ZOOM",
            "price_status": "paid",
            "registration_url": "https://luma.com/nosummlv",
            "canonical_event_id": "07f91654-c873-4f70-9b64-5dbc88ea95b4",
            "shared_event_count": 36,
        },
        {
            "city": None,
            "ring": 1,
            "label": "test (delete) [members only]",
            "degree": 3,
            "end_at": "2027-11-06T02:15:00+00:00",
            "topics": [],
            "is_past": False,
            "node_id": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "start_at": "2027-11-06T01:15:00+00:00",
            "ego_roles": ["host", "organizer"],
            "node_kind": "event",
            "venue_name": "540 Laguna St, San Francisco + 540 Cafe",
            "price_status": "free",
            "registration_url": "https://luma.com/66v378k1",
            "canonical_event_id": "22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "shared_event_count": 83,
        },
    ],
    "counts": {
        "edges": 4,
        "peers": 6,
        "events": 2,
        "topics": 0,
        "edges_total": 4,
        "peers_total": 3218,
        "events_total": 2,
        "mention_edges": 4,
    },
    "focus_id": "overview",
    "truncated": {"edges": False, "peers": True, "events": False},
    "generated_at": "2026-08-27T01:29:03.193485+00:00",
    "same_name_candidates": [],
}

_PAIR_CAPPED: dict[str, Any] = {
    "edges": [
        {
            "a": "entity:74d7e6cc-209d-471f-90f2-00a08151201f",
            "b": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "kind": "mention",
            "roles": ["host"],
            "observed_at": "2026-08-27T00:51:52.088298+00:00",
            "source_labels": ["The Commons"],
        },
        {
            "a": "entity:dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "b": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "kind": "mention",
            "roles": ["organizer"],
            "observed_at": "2026-08-27T00:51:52.088298+00:00",
            "source_labels": ["The Commons"],
        },
    ],
    "nodes": [
        {
            "ring": 0,
            "label": "Claude Community Events",
            "roles": ["organizer"],
            "degree": 75,
            "node_id": "entity:2aa228d7-56e2-4e7f-928a-6bb383b7a689",
            "entity_id": "2aa228d7-56e2-4e7f-928a-6bb383b7a689",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://claude.ai",
            "profile_url": "https://claude.ai",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Brooklyn Grange: Sunset Park and Brooklyn Navy Yard",
            "roles": ["organizer"],
            "degree": 34,
            "node_id": "entity:503e5cc5-1dec-4cd1-9350-22d1940150a4",
            "entity_id": "503e5cc5-1dec-4cd1-9350-22d1940150a4",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Accent Is A Superpower (online & offline events)",
            "roles": ["organizer"],
            "degree": 37,
            "node_id": "entity:62d0fe66-3fb6-4810-b5b2-764b8dddd6d5",
            "entity_id": "62d0fe66-3fb6-4810-b5b2-764b8dddd6d5",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://accentaccent.com/",
            "profile_url": "https://accentaccent.com/",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "The SF Commons",
            "roles": ["host"],
            "degree": 88,
            "node_id": "entity:74d7e6cc-209d-471f-90f2-00a08151201f",
            "entity_id": "74d7e6cc-209d-471f-90f2-00a08151201f",
            "node_kind": "entity",
            "entity_kind": "unknown",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Accent Accent",
            "roles": ["host"],
            "degree": 36,
            "node_id": "entity:99e5b580-0449-4958-8945-74eb62484b93",
            "entity_id": "99e5b580-0449-4958-8945-74eb62484b93",
            "node_kind": "entity",
            "entity_kind": "unknown",
            "profile_key": None,
            "profile_url": None,
            "identity_status": "source_scoped",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "The Commons",
            "roles": ["organizer"],
            "degree": 83,
            "node_id": "entity:dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "entity_id": "dede2278-3cda-4e3c-894b-f6ea6d478f3c",
            "node_kind": "entity",
            "entity_kind": "organization",
            "profile_key": "https://thesfcommons.com",
            "profile_url": "https://thesfcommons.com",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "city": None,
            "ring": 1,
            "label": "test (delete) [members only]",
            "degree": 3,
            "end_at": "2027-11-06T02:15:00+00:00",
            "topics": [],
            "is_past": False,
            "node_id": "event:22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "start_at": "2027-11-06T01:15:00+00:00",
            "ego_roles": ["host", "organizer"],
            "node_kind": "event",
            "venue_name": "540 Laguna St, San Francisco + 540 Cafe",
            "price_status": "free",
            "registration_url": "https://luma.com/66v378k1",
            "canonical_event_id": "22bbca86-d9f7-45d8-a06c-e438b10a103f",
            "shared_event_count": 83,
        },
    ],
    "counts": {
        "edges": 2,
        "peers": 6,
        "events": 1,
        "topics": 0,
        "edges_total": 2,
        "peers_total": 3218,
        "events_total": 2,
        "mention_edges": 2,
    },
    "focus_id": "overview",
    "truncated": {"edges": False, "peers": True, "events": True},
    "generated_at": "2026-08-27T01:29:03.267505+00:00",
    "same_name_candidates": [],
}

_PEOPLE_ONLY: dict[str, Any] = {
    "edges": [
        {
            "a": "entity:348bcaef-817a-464b-89e7-6b1851c02f29",
            "b": "event:5097b66d-b1c1-4d13-b37c-1fdedf1ca26d",
            "kind": "mention",
            "roles": ["host"],
            "observed_at": "2026-08-27T00:48:18.428951+00:00",
            "source_labels": ["Corgi NYC"],
        },
        {
            "a": "entity:542ee5f4-f53d-40ec-b5c0-b4a7bbd91a9e",
            "b": "event:5097b66d-b1c1-4d13-b37c-1fdedf1ca26d",
            "kind": "mention",
            "roles": ["host"],
            "observed_at": "2026-08-27T00:48:18.428951+00:00",
            "source_labels": ["Corgi NYC"],
        },
        {
            "a": "entity:57197cc7-522b-402f-ab2b-a139c73cd922",
            "b": "event:5097b66d-b1c1-4d13-b37c-1fdedf1ca26d",
            "kind": "mention",
            "roles": ["host", "organizer"],
            "observed_at": "2026-08-27T00:48:18.428951+00:00",
            "source_labels": ["Corgi NYC"],
        },
    ],
    "nodes": [
        {
            "ring": 0,
            "label": "J.H. Seow",
            "roles": ["host"],
            "degree": 10,
            "node_id": "entity:013cea5b-1c34-42e1-b19b-67e12b1b571d",
            "entity_id": "013cea5b-1c34-42e1-b19b-67e12b1b571d",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/jiaseow",
            "profile_url": "https://www.linkedin.com/in/jiaseow",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Rick Kempinski",
            "roles": ["host"],
            "degree": 12,
            "node_id": "entity:14c7b6e7-0289-451d-91a0-7603479c7547",
            "entity_id": "14c7b6e7-0289-451d-91a0-7603479c7547",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/rick-kempinski-66537243",
            "profile_url": "https://www.linkedin.com/in/rick-kempinski-66537243",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Lee Flannery",
            "roles": ["host"],
            "degree": 11,
            "node_id": "entity:2c2f1a0f-c425-44f5-a3f3-d77baec792d8",
            "entity_id": "2c2f1a0f-c425-44f5-a3f3-d77baec792d8",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/leecflannery",
            "profile_url": "https://www.linkedin.com/in/leecflannery",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Abril Zucchi",
            "roles": ["host"],
            "degree": 9,
            "node_id": "entity:348bcaef-817a-464b-89e7-6b1851c02f29",
            "entity_id": "348bcaef-817a-464b-89e7-6b1851c02f29",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/abruzucchi",
            "profile_url": "https://www.linkedin.com/in/abruzucchi",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Kristopher Floyd",
            "roles": ["host"],
            "degree": 9,
            "node_id": "entity:54078c18-cfc3-45ee-9581-dcb88c7fe517",
            "entity_id": "54078c18-cfc3-45ee-9581-dcb88c7fe517",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/kjfloyd",
            "profile_url": "https://www.linkedin.com/in/kjfloyd",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Maddie",
            "roles": ["host"],
            "degree": 11,
            "node_id": "entity:542ee5f4-f53d-40ec-b5c0-b4a7bbd91a9e",
            "entity_id": "542ee5f4-f53d-40ec-b5c0-b4a7bbd91a9e",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/madeline-e-ford",
            "profile_url": "https://www.linkedin.com/in/madeline-e-ford",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "jamie",
            "roles": ["host", "organizer"],
            "degree": 9,
            "node_id": "entity:57197cc7-522b-402f-ab2b-a139c73cd922",
            "entity_id": "57197cc7-522b-402f-ab2b-a139c73cd922",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/jamie-park16",
            "profile_url": "https://www.linkedin.com/in/jamie-park16",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Kanso",
            "roles": ["host"],
            "degree": 10,
            "node_id": "entity:5781690c-3823-4c2a-b4d0-3e01802967eb",
            "entity_id": "5781690c-3823-4c2a-b4d0-3e01802967eb",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/randy-ginsburg",
            "profile_url": "https://www.linkedin.com/in/randy-ginsburg",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Lauren Cotta",
            "roles": ["host"],
            "degree": 12,
            "node_id": "entity:626cd802-7a0b-4aaa-b36e-0bd79ec97d57",
            "entity_id": "626cd802-7a0b-4aaa-b36e-0bd79ec97d57",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/laurencotta",
            "profile_url": "https://www.linkedin.com/in/laurencotta",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Sahar Mor (Bond AI)",
            "roles": ["host"],
            "degree": 19,
            "node_id": "entity:7a49a27e-f36c-48bc-87eb-c9ab7b2965f4",
            "entity_id": "7a49a27e-f36c-48bc-87eb-c9ab7b2965f4",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/sahar-mor",
            "profile_url": "https://www.linkedin.com/in/sahar-mor",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Laura Dang",
            "roles": ["host"],
            "degree": 10,
            "node_id": "entity:ac0f929f-c5a9-4639-aabf-d3c4e1c38a1f",
            "entity_id": "ac0f929f-c5a9-4639-aabf-d3c4e1c38a1f",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/laura-t-dang",
            "profile_url": "https://www.linkedin.com/in/laura-t-dang",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "ring": 0,
            "label": "Andrew Yeung",
            "roles": ["host"],
            "degree": 14,
            "node_id": "entity:e08ce203-78c9-4233-a08b-8b0b72ea3fd5",
            "entity_id": "e08ce203-78c9-4233-a08b-8b0b72ea3fd5",
            "node_kind": "entity",
            "entity_kind": "person",
            "profile_key": "https://linkedin.com/in/andyeung",
            "profile_url": "https://www.linkedin.com/in/andyeung",
            "identity_status": "profile_verified",
            "shared_event_count": None,
        },
        {
            "city": "newyork",
            "ring": 1,
            "label": "coworking @ corgi cafe nyc",
            "degree": 4,
            "end_at": "2026-09-02T20:00:00+00:00",
            "topics": ["ai", "founders"],
            "is_past": False,
            "node_id": "event:5097b66d-b1c1-4d13-b37c-1fdedf1ca26d",
            "start_at": "2026-09-02T14:00:00+00:00",
            "ego_roles": ["host", "organizer"],
            "node_kind": "event",
            "venue_name": "121 E 27th St",
            "price_status": "free",
            "registration_url": "https://luma.com/68bh0w1e",
            "canonical_event_id": "5097b66d-b1c1-4d13-b37c-1fdedf1ca26d",
            "shared_event_count": 9,
        },
    ],
    "counts": {
        "edges": 3,
        "peers": 12,
        "events": 1,
        "topics": 0,
        "edges_total": 3,
        "peers_total": 1168,
        "events_total": 3,
        "mention_edges": 3,
    },
    "focus_id": "overview",
    "truncated": {"edges": False, "peers": True, "events": False},
    "generated_at": "2026-08-27T01:29:03.315269+00:00",
    "same_name_candidates": [],
}


def _top_hubs_payload() -> dict[str, Any]:
    """6 ranked hubs of 3,218 matching; 2 bridge events; 2 hubs in no pair."""
    return deepcopy(_TOP_HUBS)


def _pair_capped_payload() -> dict[str, Any]:
    """The same 6 hubs with the pair cap at 1: 1 of 2 connected pairs drawn."""
    return deepcopy(_PAIR_CAPPED)


def _people_only_payload() -> dict[str, Any]:
    """kind=person, 12 hubs: 3 pairs meeting at one event, so 3 pairs elect 1 representative."""
    return deepcopy(_PEOPLE_ONLY)


def _entities(graph: Any) -> list[Any]:
    return [node for node in graph.nodes if node.node_kind == "entity"]


def _bridges(graph: Any) -> list[Any]:
    return [node for node in graph.nodes if node.node_kind == "event"]


def test_overview_returns_the_ego_envelope_with_no_ego() -> None:
    for payload in (_top_hubs_payload(), _pair_capped_payload(), _people_only_payload()):
        graph = graph_from_payload(payload)

        # Not a namespaced node id.  A client that centres the viewport on focus_id must fail
        # loudly here rather than quietly centring on whichever entity happened to sort first.
        assert graph.focus_id == "overview"
        assert graph.same_name_candidates == ()
        assert graph.counts.topics == 0
        assert not any(node.node_kind == "topic" for node in graph.nodes)
        assert {edge.kind for edge in graph.edges} == {"mention"}


def test_ring_zero_is_every_ranked_hub_and_ring_one_is_the_bridge_events() -> None:
    graph = graph_from_payload(_top_hubs_payload())

    assert {node.ring for node in _entities(graph)} == {0}
    assert {node.ring for node in _bridges(graph)} == {1}
    assert len(_entities(graph)) == graph.counts.peers == 6
    assert len(_bridges(graph)) == graph.counts.events == 2
    # Each hub carries its whole-catalog degree, which is what the node radius reads off; none
    # carries a shared count, because sharing is a property of a pair and not of a hub.
    assert all(node.degree > 0 for node in _entities(graph))
    assert all(node.shared_event_count is None for node in _entities(graph))
    assert all(node.entity_id is not None for node in _entities(graph))


def test_a_hub_in_no_pair_is_still_drawn_rather_than_dropped() -> None:
    """An isolated hub is a finding about the catalog, not a row to hide.

    Two of these six share no event with any other ranked hub.  Dropping them would quietly
    redefine "top hub" as "top hub that happens to co-appear"; at the production limit of 40 that
    silently deletes 16 of them.
    """
    graph = graph_from_payload(_top_hubs_payload())
    touched = {edge.a for edge in graph.edges}

    isolated = [node for node in _entities(graph) if node.node_id not in touched]

    assert len(isolated) == 2
    assert all(node.ring == 0 for node in isolated)
    # Drawn because they are hubs by measured event count, which they still report.
    assert all(node.degree > 0 for node in isolated)


def test_the_bipartite_spine_holds_and_no_edge_reaches_an_undrawn_node() -> None:
    """entity -> event -> entity, always.  A direct entity-to-entity edge would be synthesized."""
    for payload in (_top_hubs_payload(), _pair_capped_payload(), _people_only_payload()):
        graph = graph_from_payload(payload)
        drawn = {node.node_id: node.node_kind for node in graph.nodes}

        for edge in graph.edges:
            assert drawn[edge.a] == "entity"
            assert drawn[edge.b] == "event"
            # Evidence, not a weight: every edge names the roles and the sources behind it.
            assert edge.roles
            assert edge.source_labels
            assert edge.observed_at is not None


def test_every_bridge_event_is_reached_by_at_least_two_hubs() -> None:
    """A bridge that joined one hub would not be a bridge; it would be an unexplained node."""
    for payload in (_top_hubs_payload(), _people_only_payload()):
        graph = graph_from_payload(payload)

        for bridge in _bridges(graph):
            incident = [edge for edge in graph.edges if edge.b == bridge.node_id]
            assert len(incident) >= 2
            assert len({edge.a for edge in incident}) == len(incident)


def test_each_bridge_node_discloses_the_pair_total_it_stands_for() -> None:
    """The representative contract: one node drawn, every shared event of the pair behind it."""
    graph = graph_from_payload(_top_hubs_payload())

    for bridge in _bridges(graph):
        assert bridge.canonical_event_id is not None
        # A UI rendering the node without this number is claiming the pair met once.
        assert bridge.shared_event_count is not None
        assert bridge.shared_event_count >= 1
        # The event's own whole-catalog entity count, which is a different quantity again.
        assert bridge.degree is not None


def test_counts_edges_names_the_edge_array_beside_it() -> None:
    for payload in (_top_hubs_payload(), _pair_capped_payload(), _people_only_payload()):
        graph = graph_from_payload(payload)

        assert graph.counts.edges == len(graph.edges)
        assert graph.counts.mention_edges == len(graph.edges)
        assert graph.counts.peers == len(_entities(graph))
        assert graph.counts.events == len(_bridges(graph))


def test_the_entity_cap_and_the_pair_cap_are_disclosed_independently() -> None:
    drawn = graph_from_payload(_top_hubs_payload())
    capped = graph_from_payload(_pair_capped_payload())

    # 6 of 3,218 entities drawn: the entity cap always bites on the live catalog.
    assert drawn.truncated.peers is True
    assert (drawn.counts.peers, drawn.counts.peers_total) == (6, 3218)
    # ...while the pair cap dropped nothing in the same frame.
    assert drawn.truncated.events is False
    assert (drawn.counts.events, drawn.counts.events_total) == (2, 2)

    # Same hubs, pair cap at 1.  Only the event disclosure moves.
    assert capped.counts.peers_total == drawn.counts.peers_total
    assert capped.truncated.peers is True
    assert capped.truncated.events is True
    assert (capped.counts.events, capped.counts.events_total) == (1, 2)
    # No edge cap exists: the pair cap already bounds the edge array at two per pair.
    assert capped.truncated.edges is False


def test_pairs_that_meet_at_one_event_collapse_to_one_node_without_reporting_truncation() -> None:
    """Why ``counts.events`` and ``counts.events_total`` are different quantities.

    Three people in this capture appear at a single event, producing three connected pairs that all
    elect the same representative.  The bridge set is deduplicated, so one node stands for all
    three.  ``counts.events`` names the node array beside it (1) and ``counts.events_total`` counts
    the pairs (3) -- comparing the two would report truncation on a frame where the cap dropped
    nothing, which is why ``truncated.events`` is read off the pair cap instead.
    """
    graph = graph_from_payload(_people_only_payload())

    bridges = _bridges(graph)

    assert graph.counts.events == len(bridges) == 1
    assert graph.counts.events_total == 3
    assert graph.truncated.events is False
    # One node, three hubs incident on it -- that is the collapse, visible in the edge array.
    assert len([edge for edge in graph.edges if edge.b == bridges[0].node_id]) == 3


def test_a_kind_filtered_overview_ranks_only_that_kind() -> None:
    """The front-door chips are a server-side predicate, applied before the top-N cut.

    Filtering after the cut would rank over a population the reader never chose and then shrink the
    graph to whatever survived.
    """
    graph = graph_from_payload(_people_only_payload())

    assert {node.entity_kind for node in _entities(graph)} == {"person"}
    # The matching total narrows with the filter, which is what makes the disclosure meaningful.
    assert graph.counts.peers_total == 1168
    assert graph.counts.peers == 12


def test_an_empty_overview_parses_without_inventing_a_graph() -> None:
    """A query matching nothing is a legitimate answer, not an error."""
    payload = _top_hubs_payload()
    payload["counts"] = dict.fromkeys(payload["counts"], 0)
    payload["truncated"] = dict.fromkeys(payload["truncated"], False)
    payload["nodes"] = []
    payload["edges"] = []

    graph = graph_from_payload(payload)

    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.focus_id == "overview"
    assert graph.truncated.peers is False


def test_overview_node_ids_must_stay_namespaced_and_agree_with_their_kind() -> None:
    """An unnamespaced id could smuggle a synthetic value into an ``entity_id`` position."""
    payload = _top_hubs_payload()
    payload["nodes"][0]["node_id"] = payload["nodes"][0]["node_id"].removeprefix("entity:")

    with pytest.raises(ValueError, match="does not match kind"):
        graph_from_payload(payload)

    payload = _top_hubs_payload()
    payload["edges"][0]["a"] = payload["edges"][0]["a"].removeprefix("entity:")

    with pytest.raises(ValueError, match="not namespaced"):
        graph_from_payload(payload)


def test_a_missing_required_key_is_a_value_error_not_a_key_error() -> None:
    """A capability revision is a shape change; the caller needs one exception class to catch."""
    for key in (
        "focus_id",
        "generated_at",
        "counts",
        "truncated",
        "nodes",
        "edges",
        "same_name_candidates",
    ):
        payload = _top_hubs_payload()
        del payload[key]

        with pytest.raises(ValueError, match=key):
            graph_from_payload(payload)


async def test_overview_rejects_every_out_of_range_bound_before_the_round_trip() -> None:
    """No engine is initialized in this suite, so reaching a session would raise RuntimeError.

    Every case below must raise ValueError instead, which is the proof the bound ran in Python and
    the request never borrowed one of the five pooled connections.  Each mirrors an ERRCODE
    '22023' check the capability enforces for itself, because it is also reachable from psql.
    """
    repository = PostgresCatalogEntityRepository()

    async def call(**overrides: Any) -> None:
        arguments: dict[str, Any] = {
            "query": None,
            "kinds": (),
            "identity_statuses": (),
            "entity_limit": 40,
            "pair_limit": 40,
        }
        arguments.update(overrides)
        await repository.overview(**arguments)

    for entity_limit in (0, 61):
        with pytest.raises(ValueError, match="entity limit must be between 1 and 60"):
            await call(entity_limit=entity_limit)
    for pair_limit in (0, 61):
        with pytest.raises(ValueError, match="pair limit must be between 1 and 60"):
            await call(pair_limit=pair_limit)
    with pytest.raises(ValueError, match="query is invalid"):
        await call(query="a" * 161)
    with pytest.raises(ValueError, match="query is invalid"):
        await call(query="drop\x00table")
    with pytest.raises(ValueError, match="query is invalid"):
        await call(query="san\x7ffrancisco")
    with pytest.raises(ValueError, match="kind filter is invalid"):
        await call(kinds=("person", "organization", "unknown", "venue"))
    with pytest.raises(ValueError, match="kind filter is invalid"):
        await call(kinds=("attendee",))
    with pytest.raises(ValueError, match="identity filter is invalid"):
        await call(identity_statuses=("profile_verified", "source_scoped", "unreviewed"))
    with pytest.raises(ValueError, match="identity filter is invalid"):
        await call(identity_statuses=("merged",))


async def test_repeated_chip_selections_are_deduplicated_before_the_cardinality_check() -> None:
    """The Python bound must run on the array the SQL will receive, not on the caller's tuple.

    The capability counts the deduplicated array, so validating the raw tuple would reject a
    request it accepts -- a spurious 4xx for a reader who clicked one chip four times.  Reaching a
    session raises ``RuntimeError`` here, which is the proof validation let the request through.
    """
    repository = PostgresCatalogEntityRepository()

    with pytest.raises(RuntimeError):
        await repository.overview(
            query=None,
            kinds=("person", "person", "person", "person"),
            identity_statuses=("source_scoped",) * 5,
            entity_limit=40,
            pair_limit=40,
        )
