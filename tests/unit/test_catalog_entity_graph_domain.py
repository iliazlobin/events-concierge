"""Adversarial parsing coverage for the bipartite ego graph and the entity directory.

Every payload below is one live capture, whole.  Each was taken from
``fn_get_catalog_entity_graph_v1`` or ``fn_get_catalog_entity_directory_v1`` against the running
catalog at small limits rather than at production limits and then pasted unedited, so nothing here
has been trimmed into a shape the database never emits.  The one consequence worth stating: the
``counts`` block describes exactly the arrays beside it, which is what lets these tests assert
``counts.edges == len(graph.edges)`` and catch a capability revision that renames a quantity.

Four populations are covered deliberately, because the graph's whole premise is that the empty state
is the exception:

* a populated hub (17 events, 2 drawn, with peers, topics and both edge kinds);
* an isolated entity -- 11.5% of the catalog -- whose peer ring is empty while its event ring and
  its topic ring are both populated, which is what an isolated entity actually looks like;
* an entity that has an exact-name review candidate, the one shape the other captures cannot show;
* one ranked directory row with its unfiltered totals and its coverage caveat.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from events_concierge.adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from events_concierge.adapters.postgres.catalog_entity_graph import (
    directory_from_payload,
    graph_from_payload,
)

_EGO = "98744dc7-e452-42b6-a9f6-648b02a6e734"
_PEER = "302e1ba1-f4df-41e2-8377-5275cb414fef"
_EVENT = "328d895f-96ea-4666-8fc6-4833891f4c90"
_ISOLATED = "5892dbb9-a9a9-4b65-b5b1-acf798d30411"
_NAMESAKE = "348bcaef-817a-464b-89e7-6b1851c02f29"


def _hub_payload() -> dict[str, Any]:
    """A populated hub, verbatim: 2 of 17 events, 2 peers, 2 topics, both edge kinds."""
    return deepcopy(
        {
            "edges": [
                {
                    "a": f"entity:{_PEER}",
                    "b": f"event:{_EVENT}",
                    "kind": "mention",
                    "roles": [
                        "host",
                    ],
                    "observed_at": "2026-08-25T01:21:54.328422+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": f"entity:{_EGO}",
                    "b": f"event:{_EVENT}",
                    "kind": "mention",
                    "roles": [
                        "organizer",
                    ],
                    "observed_at": "2026-08-25T01:21:54.328422+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": f"entity:{_EGO}",
                    "b": "event:5993b5eb-3b76-4200-9cdb-e89783a08465",
                    "kind": "mention",
                    "roles": [
                        "organizer",
                    ],
                    "observed_at": "2026-08-24T19:46:30.996317+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": "entity:fef63671-6fea-4670-93de-b1db120e0ab8",
                    "b": "event:5993b5eb-3b76-4200-9cdb-e89783a08465",
                    "kind": "mention",
                    "roles": [
                        "host",
                    ],
                    "observed_at": "2026-08-24T19:46:30.996317+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": f"entity:{_EGO}",
                    "b": "topic:technology",
                    "kind": "topic",
                    "roles": [],
                    "observed_at": None,
                    "source_labels": [],
                },
                {
                    "a": f"entity:{_EGO}",
                    "b": "topic:networking",
                    "kind": "topic",
                    "roles": [],
                    "observed_at": None,
                    "source_labels": [],
                },
            ],
            "nodes": [
                {
                    "ring": 2,
                    "label": "Noah Glaser",
                    "degree": 4,
                    "node_id": f"entity:{_PEER}",
                    "entity_id": _PEER,
                    "node_kind": "entity",
                    "entity_kind": "unknown",
                    "profile_key": None,
                    "profile_url": None,
                    "identity_status": "source_scoped",
                    "shared_event_count": 1,
                },
                {
                    "ring": 0,
                    "label": "Noisebridge Hackerspace",
                    "roles": [
                        "organizer",
                    ],
                    "degree": 17,
                    "node_id": f"entity:{_EGO}",
                    "entity_id": _EGO,
                    "node_kind": "entity",
                    "entity_kind": "organization",
                    "profile_key": None,
                    "profile_url": None,
                    "identity_status": "source_scoped",
                    "shared_event_count": None,
                },
                {
                    "ring": 2,
                    "label": "Loren McIntyre",
                    "degree": 2,
                    "node_id": "entity:fef63671-6fea-4670-93de-b1db120e0ab8",
                    "entity_id": "fef63671-6fea-4670-93de-b1db120e0ab8",
                    "node_kind": "entity",
                    "entity_kind": "unknown",
                    "profile_key": None,
                    "profile_url": None,
                    "identity_status": "source_scoped",
                    "shared_event_count": 1,
                },
                {
                    "city": "sanfrancisco",
                    "ring": 1,
                    "label": "Build a remote controlled led light strip",
                    "degree": 2,
                    "end_at": "2026-08-25T04:00:00+00:00",
                    "topics": [
                        "sports",
                        "technology",
                    ],
                    "is_past": True,
                    "node_id": f"event:{_EVENT}",
                    "start_at": "2026-08-25T02:00:00+00:00",
                    "ego_roles": [
                        "organizer",
                    ],
                    "node_kind": "event",
                    "venue_name": "Noisebridge",
                    "price_status": "unknown",
                    "registration_url": "https://www.meetup.com/noisebridge/events/315930131/",
                    "canonical_event_id": _EVENT,
                },
                {
                    "city": "sanfrancisco",
                    "ring": 1,
                    "label": "All things software infrastructure",
                    "degree": 2,
                    "end_at": "2026-08-25T03:30:00+00:00",
                    "topics": [
                        "networking",
                        "technology",
                    ],
                    "is_past": True,
                    "node_id": "event:5993b5eb-3b76-4200-9cdb-e89783a08465",
                    "start_at": "2026-08-25T02:00:00+00:00",
                    "ego_roles": [
                        "organizer",
                    ],
                    "node_kind": "event",
                    "venue_name": "Noisebridge",
                    "price_status": "unknown",
                    "registration_url": "https://www.meetup.com/noisebridge/events/315983282/",
                    "canonical_event_id": "5993b5eb-3b76-4200-9cdb-e89783a08465",
                },
                {
                    "ring": 3,
                    "label": "networking",
                    "degree": 1,
                    "node_id": "topic:networking",
                    "node_kind": "topic",
                },
                {
                    "ring": 3,
                    "label": "technology",
                    "degree": 2,
                    "node_id": "topic:technology",
                    "node_kind": "topic",
                },
            ],
            "counts": {
                "edges": 6,
                "peers": 2,
                "events": 2,
                "topics": 2,
                "edges_total": 4,
                "peers_total": 2,
                "events_total": 17,
                "mention_edges": 4,
            },
            "focus_id": f"entity:{_EGO}",
            "truncated": {
                "edges": False,
                "peers": False,
                "events": True,
            },
            "generated_at": "2026-08-27T00:01:46.28177+00:00",
            "same_name_candidates": [],
        }
    )


def _isolated_payload() -> dict[str, Any]:
    """An isolated entity, verbatim: no peers, but rings 1 and 3 are both populated."""
    return deepcopy(
        {
            "edges": [
                {
                    "a": f"entity:{_ISOLATED}",
                    "b": "event:411f0922-7746-4efb-bef0-73b2c5325e07",
                    "kind": "mention",
                    "roles": [
                        "organizer",
                    ],
                    "observed_at": "2026-08-16T22:05:29.072119+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": f"entity:{_ISOLATED}",
                    "b": "event:5662a1dc-c77b-4dbc-a247-fe4608d2ba7c",
                    "kind": "mention",
                    "roles": [
                        "organizer",
                    ],
                    "observed_at": "2026-08-16T22:05:29.072119+00:00",
                    "source_labels": [
                        "Meetup San Francisco",
                    ],
                },
                {
                    "a": f"entity:{_ISOLATED}",
                    "b": "topic:founders",
                    "kind": "topic",
                    "roles": [],
                    "observed_at": None,
                    "source_labels": [],
                },
                {
                    "a": f"entity:{_ISOLATED}",
                    "b": "topic:ai",
                    "kind": "topic",
                    "roles": [],
                    "observed_at": None,
                    "source_labels": [],
                },
            ],
            "nodes": [
                {
                    "ring": 0,
                    "label": "staaake",
                    "roles": [
                        "organizer",
                    ],
                    "degree": 6,
                    "node_id": f"entity:{_ISOLATED}",
                    "entity_id": _ISOLATED,
                    "node_kind": "entity",
                    "entity_kind": "organization",
                    "profile_key": None,
                    "profile_url": None,
                    "identity_status": "source_scoped",
                    "shared_event_count": None,
                },
                {
                    "city": "sanfrancisco",
                    "ring": 1,
                    "label": "Tech Founders \u2014 Power Walk",
                    "degree": 1,
                    "end_at": "2026-08-17T00:00:00+00:00",
                    "topics": [
                        "founders",
                    ],
                    "is_past": True,
                    "node_id": "event:411f0922-7746-4efb-bef0-73b2c5325e07",
                    "start_at": "2026-08-16T23:00:00+00:00",
                    "ego_roles": [
                        "organizer",
                    ],
                    "node_kind": "event",
                    "venue_name": "Starbucks Coffee Company",
                    "price_status": "unknown",
                    "registration_url": "https://www.meetup.com/staaake/events/316054916/",
                    "canonical_event_id": "411f0922-7746-4efb-bef0-73b2c5325e07",
                },
                {
                    "city": "sanfrancisco",
                    "ring": 1,
                    "label": "The Final Summer Founders Rooftop Series, AI/Tech \u2014 Reverse Network Meet",
                    "degree": 1,
                    "end_at": "2026-08-17T01:30:00+00:00",
                    "topics": [
                        "ai",
                        "founders",
                    ],
                    "is_past": True,
                    "node_id": "event:5662a1dc-c77b-4dbc-a247-fe4608d2ba7c",
                    "start_at": "2026-08-17T00:30:00+00:00",
                    "ego_roles": [
                        "organizer",
                    ],
                    "node_kind": "event",
                    "venue_name": "San Francisco",
                    "price_status": "unknown",
                    "registration_url": "https://www.meetup.com/staaake/events/315783815/",
                    "canonical_event_id": "5662a1dc-c77b-4dbc-a247-fe4608d2ba7c",
                },
                {
                    "ring": 3,
                    "label": "ai",
                    "degree": 1,
                    "node_id": "topic:ai",
                    "node_kind": "topic",
                },
                {
                    "ring": 3,
                    "label": "founders",
                    "degree": 2,
                    "node_id": "topic:founders",
                    "node_kind": "topic",
                },
            ],
            "counts": {
                "edges": 4,
                "peers": 0,
                "events": 2,
                "topics": 2,
                "edges_total": 2,
                "peers_total": 0,
                "events_total": 6,
                "mention_edges": 2,
            },
            "focus_id": f"entity:{_ISOLATED}",
            "truncated": {
                "edges": False,
                "peers": False,
                "events": True,
            },
            "generated_at": "2026-08-27T00:01:46.335822+00:00",
            "same_name_candidates": [],
        }
    )


def _same_name_payload() -> dict[str, Any]:
    """The one shape the hub capture cannot show: a non-empty exact-name review list."""
    return deepcopy(
        {
            "edges": [
                {
                    "a": f"entity:{_NAMESAKE}",
                    "b": "event:16e41686-ed21-4f5d-995d-7fbab5f568c8",
                    "kind": "mention",
                    "roles": [
                        "host",
                    ],
                    "observed_at": "2026-08-21T20:31:23.785247+00:00",
                    "source_labels": [
                        "Luma New York",
                    ],
                },
                {
                    "a": "entity:922d552f-d900-4be0-85f6-058a1a4644d2",
                    "b": "event:16e41686-ed21-4f5d-995d-7fbab5f568c8",
                    "kind": "mention",
                    "roles": [
                        "partner",
                    ],
                    "observed_at": "2026-08-21T20:31:23.785247+00:00",
                    "source_labels": [
                        "Luma New York",
                    ],
                },
                {
                    "a": f"entity:{_NAMESAKE}",
                    "b": "topic:community",
                    "kind": "topic",
                    "roles": [],
                    "observed_at": None,
                    "source_labels": [],
                },
            ],
            "nodes": [
                {
                    "ring": 0,
                    "label": "Abril Zucchi",
                    "roles": [
                        "host",
                    ],
                    "degree": 7,
                    "node_id": f"entity:{_NAMESAKE}",
                    "entity_id": _NAMESAKE,
                    "node_kind": "entity",
                    "entity_kind": "person",
                    "profile_key": "https://linkedin.com/in/abruzucchi",
                    "profile_url": "https://www.linkedin.com/in/abruzucchi",
                    "identity_status": "profile_verified",
                    "shared_event_count": None,
                },
                {
                    "ring": 2,
                    "label": "Antimetal",
                    "degree": 1,
                    "node_id": "entity:922d552f-d900-4be0-85f6-058a1a4644d2",
                    "entity_id": "922d552f-d900-4be0-85f6-058a1a4644d2",
                    "node_kind": "entity",
                    "entity_kind": "organization",
                    "profile_key": None,
                    "profile_url": None,
                    "identity_status": "source_scoped",
                    "shared_event_count": 1,
                },
                {
                    "city": "newyork",
                    "ring": 1,
                    "label": "Corgi Cafe: NYC Grand Opening",
                    "degree": 14,
                    "end_at": "2026-08-22T01:00:00+00:00",
                    "topics": [
                        "community",
                        "founders",
                    ],
                    "is_past": True,
                    "node_id": "event:16e41686-ed21-4f5d-995d-7fbab5f568c8",
                    "start_at": "2026-08-21T22:30:00+00:00",
                    "ego_roles": [
                        "host",
                    ],
                    "node_kind": "event",
                    "venue_name": "Manhattan",
                    "price_status": "free",
                    "registration_url": "https://luma.com/5z095tev",
                    "canonical_event_id": "16e41686-ed21-4f5d-995d-7fbab5f568c8",
                },
                {
                    "ring": 3,
                    "label": "community",
                    "degree": 1,
                    "node_id": "topic:community",
                    "node_kind": "topic",
                },
            ],
            "counts": {
                "edges": 3,
                "peers": 1,
                "events": 1,
                "topics": 1,
                "edges_total": 2,
                "peers_total": 13,
                "events_total": 7,
                "mention_edges": 2,
            },
            "focus_id": f"entity:{_NAMESAKE}",
            "truncated": {
                "edges": False,
                "peers": True,
                "events": True,
            },
            "generated_at": "2026-08-27T00:01:59.275818+00:00",
            "same_name_candidates": [
                {
                    "kind": "unknown",
                    "entity_id": "4208db9d-08cf-40e6-b9ac-f4c5d6a20bcb",
                    "event_count": 1,
                    "display_name": "Abril Zucchi",
                    "identity_status": "source_scoped",
                },
            ],
        }
    )


def _directory_payload() -> dict[str, Any]:
    """One ranked row with the unfiltered totals and the coverage caveat beside it."""
    return deepcopy(
        {
            "hubs": [
                {
                    "kind": "organization",
                    "roles": [
                        "organizer",
                    ],
                    "top_city": "sanfrancisco",
                    "entity_id": _EGO,
                    "peer_count": 6,
                    "event_count": 17,
                    "profile_key": None,
                    "profile_url": None,
                    "display_name": "Noisebridge Hackerspace",
                    "source_count": 1,
                    "last_event_at": "2026-08-25T02:00:00+00:00",
                    "upcoming_count": 0,
                    "identity_status": "source_scoped",
                },
            ],
            "totals": {
                "entity_count": 3036,
                "person_count": 1036,
                "scoped_count": 1776,
                "unknown_count": 893,
                "verified_count": 1260,
                "organization_count": 1107,
            },
            "matched": 1,
            "coverage": {
                "events_total": 39771,
                "mention_count": 5059,
                "events_with_entities": 1925,
            },
            "generated_at": "2026-08-27T00:01:59.325432+00:00",
        }
    )


def test_populated_hub_parses_every_ring_and_reports_its_truncation() -> None:
    graph = graph_from_payload(_hub_payload())

    assert graph.focus_id == f"entity:{_EGO}"
    assert graph.generated_at == datetime(2026, 8, 27, 0, 1, 46, 281770, tzinfo=UTC)
    assert graph.counts.events == 2
    assert graph.counts.events_total == 17
    assert graph.truncated.events is True
    assert graph.truncated.peers is False
    assert {node.ring for node in graph.nodes} == {0, 1, 2, 3}

    ego = next(node for node in graph.nodes if node.ring == 0)
    assert ego.entity_id == UUID(_EGO)
    assert ego.entity_kind == "organization"
    assert ego.identity_status == "source_scoped"
    # Degree is the whole-catalog connection count, not the 2 events this frame drew.
    assert ego.degree == 17
    assert ego.roles == ("organizer",)
    assert ego.shared_event_count is None
    assert ego.canonical_event_id is None

    event = next(node for node in graph.nodes if node.canonical_event_id == UUID(_EVENT))
    assert event.node_kind == "event"
    assert event.start_at == datetime(2026, 8, 25, 2, tzinfo=UTC)
    assert event.end_at == datetime(2026, 8, 25, 4, tzinfo=UTC)
    assert event.is_past is True
    assert event.venue_name == "Noisebridge"
    assert event.city == "sanfrancisco"
    assert event.topics == ("sports", "technology")
    assert event.ego_roles == ("organizer",)
    assert event.registration_url is not None
    assert event.entity_id is None

    peer = next(node for node in graph.nodes if node.entity_id == UUID(_PEER))
    assert peer.ring == 2
    assert peer.shared_event_count == 1

    topic = next(node for node in graph.nodes if node.node_id == "topic:technology")
    assert topic.node_kind == "topic"
    assert topic.label == "technology"
    assert topic.degree == 2
    assert topic.entity_id is None


def test_counts_edges_names_the_array_beside_it_on_every_capture() -> None:
    """The count a legend or a truncation banner reads must be the count of what was drawn.

    ``counts.edges`` is the emitted array -- mention edges plus one topic edge per topic node --
    while ``mention_edges`` is the capped subset ``edges_total`` compares against.  The body
    installed by ``0156`` reported the mention count under the name ``edges``, which understated
    every graph carrying a topic by exactly the topic count; ``0165`` repairs it and the parser
    reconstructs the split for either body.  This assertion is what fails if that drift returns.
    """
    for payload in (_hub_payload(), _isolated_payload(), _same_name_payload()):
        graph = graph_from_payload(payload)

        assert graph.counts.edges == len(graph.edges)
        assert graph.counts.mention_edges == sum(
            1 for edge in graph.edges if edge.kind == "mention"
        )
        assert graph.counts.edges == graph.counts.mention_edges + graph.counts.topics
        assert graph.counts.topics == sum(1 for node in graph.nodes if node.node_kind == "topic")


def test_hub_edges_carry_their_evidence_and_no_weight() -> None:
    graph = graph_from_payload(_hub_payload())

    mention = next(edge for edge in graph.edges if edge.kind == "mention")
    assert mention.a.startswith("entity:")
    assert mention.b.startswith("event:")
    assert mention.roles == ("host",)
    assert mention.source_labels == ("Meetup San Francisco",)
    assert mention.observed_at == datetime(2026, 8, 25, 1, 21, 54, 328422, tzinfo=UTC)

    topic_edge = next(edge for edge in graph.edges if edge.kind == "topic")
    assert topic_edge.roles == ()
    assert topic_edge.source_labels == ()
    assert topic_edge.observed_at is None

    # Bipartite by construction: no drawn line ever joins two entities directly.
    assert not any(
        edge.a.startswith("entity:") and edge.b.startswith("entity:") for edge in graph.edges
    )


def test_same_name_candidates_are_review_rows_not_merges() -> None:
    graph = graph_from_payload(_same_name_payload())

    assert len(graph.same_name_candidates) == 1
    candidate = graph.same_name_candidates[0]
    assert candidate.entity_id == UUID("4208db9d-08cf-40e6-b9ac-f4c5d6a20bcb")
    assert candidate.display_name == "Abril Zucchi"
    assert candidate.kind == "unknown"
    assert candidate.event_count == 1
    # The candidate is a separate entity; nothing folds it into the ego's rings.
    assert candidate.entity_id not in {node.entity_id for node in graph.nodes}
    # And the ego it is a namesake of is the one the capture centred on, not the candidate.
    assert graph.focus_id == f"entity:{_NAMESAKE}"


def test_hub_without_a_namesake_reports_an_empty_review_list() -> None:
    assert graph_from_payload(_hub_payload()).same_name_candidates == ()


def test_isolated_entity_keeps_its_event_and_topic_rings_and_reports_zero_peers() -> None:
    """The 11.5% case, exactly as the database returns it.

    An isolated entity is isolated in *co-mention*: it shares no event with another entity.  Its
    events still carry topics, so ring 3 is populated and ring 2 is not -- which is why this fixture
    asserts a topic ring rather than the two-ring shape a hand-written empty case would produce.
    """
    graph = graph_from_payload(_isolated_payload())

    assert graph.counts.peers == 0
    assert graph.counts.peers_total == 0
    assert graph.truncated.peers is False
    assert not any(node.ring == 2 for node in graph.nodes)
    # The event ring is never empty: every entity carries at least one mention by construction.
    assert [node.ring for node in graph.nodes] == [0, 1, 1, 3, 3]
    assert graph.counts.topics == 2
    assert sum(1 for edge in graph.edges if edge.kind == "topic") == 2
    assert sum(1 for edge in graph.edges if edge.kind == "mention") == 2
    # Every topic edge leaves the ego, so an isolated entity still reads as a centre.
    assert {edge.a for edge in graph.edges} == {f"entity:{_ISOLATED}"}


def test_empty_rings_and_absent_optionals_parse_without_inventing_values() -> None:
    payload = _isolated_payload()
    payload["nodes"] = []
    payload["edges"] = []
    payload["counts"] = {
        "events": 0,
        "events_total": 0,
        "peers": 0,
        "peers_total": 0,
        "topics": 0,
        "edges": 0,
        "mention_edges": 0,
        "edges_total": 0,
    }

    graph = graph_from_payload(payload)

    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.same_name_candidates == ()
    assert graph.counts.edges == 0


def test_node_optionals_are_none_rather_than_defaulted_when_the_row_omits_them() -> None:
    payload = _isolated_payload()
    payload["nodes"] = [
        {
            "node_id": "topic:ai",
            "node_kind": "topic",
            "ring": 3,
            "label": "ai",
            "degree": 3,
        }
    ]

    node = graph_from_payload(payload).nodes[0]

    assert node.entity_id is None
    assert node.entity_kind is None
    assert node.identity_status is None
    assert node.profile_url is None
    assert node.profile_key is None
    assert node.canonical_event_id is None
    assert node.start_at is None
    assert node.is_past is None
    assert node.shared_event_count is None
    assert node.topics == ()
    assert node.ego_roles == ()
    assert node.roles == ()


def test_verified_profile_fields_survive_and_carry_no_confidence_signal() -> None:
    node = next(node for node in graph_from_payload(_same_name_payload()).nodes if node.ring == 0)

    assert node.identity_status == "profile_verified"
    assert node.profile_url == "https://www.linkedin.com/in/abruzucchi"
    assert node.profile_key == "https://linkedin.com/in/abruzucchi"
    # A profile URL is shown with the source that asserted it and nothing else: there is no
    # confidence, doubt or similarity value anywhere in this read model.
    assert not any("confidence" in field for field in type(node).__slots__)


def test_a_capability_body_without_mention_edges_still_parses_to_the_same_numbers() -> None:
    """``0156``'s installed body reported only the mention count, under the name ``edges``.

    Reconstructing the split rather than trusting the key is what makes the two bodies agree: the
    parser must produce the same eight numbers whichever one answered.
    """
    repaired = graph_from_payload(_hub_payload())

    legacy_payload = _hub_payload()
    legacy_payload["counts"]["edges"] = legacy_payload["counts"].pop("mention_edges")

    legacy = graph_from_payload(legacy_payload)

    assert legacy.counts == repaired.counts
    assert legacy.counts.edges == 6
    assert legacy.counts.mention_edges == 4
    assert legacy.counts.edges == len(legacy.edges)


def test_a_missing_required_key_is_a_value_error_not_a_key_error() -> None:
    """A shape change must arrive as the one exception class the caller catches."""
    for key in ("focus_id", "counts", "truncated", "nodes", "edges", "same_name_candidates"):
        payload = _hub_payload()
        del payload[key]
        with pytest.raises(ValueError, match=f"missing {key!r}"):
            graph_from_payload(payload)

    for key in ("events", "events_total", "peers", "peers_total", "topics", "edges_total"):
        payload = _hub_payload()
        del payload["counts"][key]
        with pytest.raises(ValueError, match=f"missing {key!r}"):
            graph_from_payload(payload)

    for key in ("totals", "coverage", "matched", "hubs"):
        payload = _directory_payload()
        del payload[key]
        with pytest.raises(ValueError, match=f"missing {key!r}"):
            directory_from_payload(payload)

    payload = _directory_payload()
    del payload["hubs"][0]["event_count"]
    with pytest.raises(ValueError, match="missing 'event_count'"):
        directory_from_payload(payload)


def test_node_ids_must_be_namespaced_and_must_agree_with_their_kind() -> None:
    unnamespaced = _isolated_payload()
    unnamespaced["nodes"][0]["node_id"] = _ISOLATED
    with pytest.raises(ValueError, match="does not match kind"):
        graph_from_payload(unnamespaced)

    mismatched = _isolated_payload()
    mismatched["nodes"][0]["node_kind"] = "event"
    with pytest.raises(ValueError, match="does not match kind"):
        graph_from_payload(mismatched)

    unknown = _isolated_payload()
    unknown["nodes"][0]["node_kind"] = "attendee"
    with pytest.raises(ValueError, match="unknown catalog entity graph node kind"):
        graph_from_payload(unknown)

    bare_edge = _isolated_payload()
    bare_edge["edges"][0]["a"] = _ISOLATED
    with pytest.raises(ValueError, match="not namespaced"):
        graph_from_payload(bare_edge)

    unknown_edge = _isolated_payload()
    unknown_edge["edges"][0]["kind"] = "attends"
    with pytest.raises(ValueError, match="unknown catalog entity graph edge kind"):
        graph_from_payload(unknown_edge)


def test_malformed_scalars_are_rejected_rather_than_coerced() -> None:
    wrong_type = _isolated_payload()
    wrong_type["counts"]["events"] = "1"
    with pytest.raises(ValueError, match="not an integer"):
        graph_from_payload(wrong_type)

    wrong_kind = _isolated_payload()
    wrong_kind["nodes"][0]["entity_kind"] = "venue"
    with pytest.raises(ValueError, match="unknown catalog entity kind"):
        graph_from_payload(wrong_kind)

    wrong_status = _isolated_payload()
    wrong_status["nodes"][0]["identity_status"] = "name_matched"
    with pytest.raises(ValueError, match="unknown catalog entity identity status"):
        graph_from_payload(wrong_status)

    with pytest.raises(ValueError, match="not an object"):
        graph_from_payload([])


def test_graph_parses_from_the_raw_json_text_a_plain_client_returns() -> None:
    graph = graph_from_payload(json.dumps(_isolated_payload()))

    assert graph.focus_id == f"entity:{_ISOLATED}"
    assert len(graph.nodes) == 5


def test_directory_parses_its_ranking_totals_and_coverage_caveat() -> None:
    directory = directory_from_payload(_directory_payload())

    assert directory.generated_at == datetime(2026, 8, 27, 0, 1, 59, 325432, tzinfo=UTC)
    assert directory.matched == 1
    assert directory.totals.entity_count == 3036
    assert directory.totals.person_count == 1036
    assert directory.totals.organization_count == 1107
    assert directory.totals.unknown_count == 893
    assert directory.totals.verified_count == 1260
    assert directory.totals.scoped_count == 1776
    # Coverage is the honesty contract and travels with the ranking it qualifies.
    assert directory.coverage.events_with_entities == 1925
    assert directory.coverage.events_total == 39771
    assert directory.coverage.mention_count == 5059

    hub = directory.hubs[0]
    assert hub.entity_id == UUID(_EGO)
    assert hub.display_name == "Noisebridge Hackerspace"
    assert hub.kind == "organization"
    assert hub.identity_status == "source_scoped"
    assert hub.profile_url is None
    assert hub.profile_key is None
    assert hub.event_count == 17
    assert hub.upcoming_count == 0
    assert hub.peer_count == 6
    assert hub.source_count == 1
    assert hub.roles == ("organizer",)
    assert hub.top_city == "sanfrancisco"
    assert hub.last_event_at == datetime(2026, 8, 25, 2, tzinfo=UTC)


def test_directory_with_no_matches_still_reports_totals_and_coverage() -> None:
    payload = _directory_payload()
    payload["hubs"] = []
    payload["matched"] = 0

    directory = directory_from_payload(payload)

    assert directory.hubs == ()
    assert directory.matched == 0
    assert directory.totals.entity_count == 3036
    assert directory.coverage.events_total == 39771


def test_directory_hub_carries_no_confidence_signal() -> None:
    hub = directory_from_payload(_directory_payload()).hubs[0]

    assert not any("confidence" in field for field in type(hub).__slots__)


async def test_graph_rejects_every_out_of_range_bound_before_the_round_trip() -> None:
    repository = PostgresCatalogEntityRepository()
    entity_id = UUID(_EGO)

    # No engine is initialized in this suite, so reaching a session at all would raise
    # RuntimeError; every case below must raise ValueError instead.
    for event_limit in (0, 25):
        with pytest.raises(ValueError, match="event limit must be between 1 and 24"):
            await repository.graph(entity_id, event_limit=event_limit)
    for peer_limit in (0, 49):
        with pytest.raises(ValueError, match="peer limit must be between 1 and 48"):
            await repository.graph(entity_id, peer_limit=peer_limit)
    for topic_limit in (-1, 7):
        with pytest.raises(ValueError, match="topic limit must be between 0 and 6"):
            await repository.graph(entity_id, topic_limit=topic_limit)


async def test_directory_rejects_every_out_of_range_bound_before_the_round_trip() -> None:
    repository = PostgresCatalogEntityRepository()

    async def call(**overrides: Any) -> None:
        arguments: dict[str, Any] = {
            "query": None,
            "kinds": (),
            "city_norms": (),
            "limit": 48,
            "min_events": 1,
        }
        arguments.update(overrides)
        await repository.directory(**arguments)

    for limit in (0, 61):
        with pytest.raises(ValueError, match="limit must be between 1 and 60"):
            await call(limit=limit)
    for min_events in (0, 11):
        with pytest.raises(ValueError, match="minimum must be between 1 and 10"):
            await call(min_events=min_events)
    with pytest.raises(ValueError, match="query is invalid"):
        await call(query="a" * 161)
    with pytest.raises(ValueError, match="query is invalid"):
        await call(query="drop\x00table")
    with pytest.raises(ValueError, match="kind filter is invalid"):
        await call(kinds=("person", "organization", "unknown", "venue"))
    with pytest.raises(ValueError, match="kind filter is invalid"):
        await call(kinds=("attendee",))
    with pytest.raises(ValueError, match="city filter is invalid"):
        await call(city_norms=tuple(f"city{index}" for index in range(9)))
    with pytest.raises(ValueError, match="city filter is invalid"):
        await call(city_norms=("x" * 65,))
    with pytest.raises(ValueError, match="city filter is invalid"):
        await call(city_norms=("san\x7ffrancisco",))


async def test_repeated_filter_selections_are_deduplicated_before_the_cardinality_check() -> None:
    """The Python bound must run on the array the SQL will receive, not on the caller's tuple.

    ``cardinality(coalesce(p_kinds,'{}'::text[])) > 3`` sees the deduplicated array, so validating
    the raw tuple would reject a request the capability accepts -- a spurious 4xx for a reader who
    clicked one chip four times.  Reaching a session raises ``RuntimeError`` in this suite, which is
    the proof that validation let the request through.
    """
    repository = PostgresCatalogEntityRepository()

    with pytest.raises(RuntimeError):
        await repository.directory(
            query=None,
            kinds=("person", "person", "person", "person"),
            city_norms=tuple("sanfrancisco" for _ in range(9)),
            limit=48,
            min_events=1,
        )
