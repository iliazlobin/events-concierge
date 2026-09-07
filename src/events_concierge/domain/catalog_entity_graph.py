"""Ego-first bipartite read models for the public entity explorer.

Every line the explorer draws is a literal ``catalog_entity_event_mentions`` row: an entity is
joined to a peer *through* the event that records them both, never by a synthesized
entity-to-entity relation.  That is why :class:`CatalogEntityGraphEdge` carries the role and the
source labels of the mention it stands for and carries no weight — a mention is a fact, not a
score — and why no value in this module is a similarity, a confidence or a centrality.

Names never appear here as identity.  :class:`CatalogEntitySameNameCandidate` is the one place a
name comparison surfaces at all, and it is a review candidate on an exact normalized-name match:
surfaced, never merged, never used as a join key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from .catalog_entities import CatalogEntityIdentityStatus, CatalogEntityKind

CatalogEntityGraphNodeKind = Literal["entity", "event", "topic"]
CatalogEntityGraphEdgeKind = Literal["mention", "topic"]


@dataclass(frozen=True, slots=True)
class CatalogEntityGraphNode:
    """One drawn node.

    ``entity_id`` stays the opaque catalog uuid; ``node_id`` is a render-time namespace minted per
    request (``entity:``/``event:``/``topic:``) and never persisted, so a synthetic id can never
    occupy an ``entity_id`` position.  ``degree`` is the node's whole-catalog connection count, not
    a property of this frame: an organizer with 17 events reads 17 here whether or not this ego
    request drew all 17.
    """

    node_id: str
    node_kind: CatalogEntityGraphNodeKind
    ring: int
    label: str
    degree: int
    entity_id: UUID | None = None
    entity_kind: CatalogEntityKind | None = None
    identity_status: CatalogEntityIdentityStatus | None = None
    profile_url: str | None = None
    profile_key: str | None = None
    canonical_event_id: UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    is_past: bool | None = None
    venue_name: str | None = None
    city: str | None = None
    price_status: str | None = None
    topics: tuple[str, ...] = ()
    ego_roles: tuple[str, ...] = ()
    registration_url: str | None = None
    shared_event_count: int | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogEntityGraphEdge:
    """A recorded mention, or the ego's tie to one of its own top topics.

    There is deliberately no weight field.  A ``mention`` edge stands for one or more
    ``catalog_entity_event_mentions`` rows aggregated onto the (entity, event) pair, so its
    evidence is the roles and the source labels it carries.
    """

    a: str
    b: str
    kind: CatalogEntityGraphEdgeKind
    roles: tuple[str, ...]
    source_labels: tuple[str, ...]
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogEntityGraphCounts:
    """What was drawn against what matched.

    ``edges`` counts the emitted edge array (mention edges plus one topic edge per topic node),
    while ``mention_edges`` is the capped mention subset that ``edges_total`` and
    :attr:`CatalogEntityGraphTruncation.edges` compare against.  Topic edges derive from the
    already-capped event set and are never truncated.
    """

    events: int
    events_total: int
    peers: int
    peers_total: int
    topics: int
    edges: int
    mention_edges: int
    edges_total: int


@dataclass(frozen=True, slots=True)
class CatalogEntityGraphTruncation:
    """Whether a cap hid anything.  Truncation is reported, never silent."""

    events: bool
    peers: bool
    edges: bool


@dataclass(frozen=True, slots=True)
class CatalogEntitySameNameCandidate:
    """Another entity whose normalized name is byte-identical to the ego's.

    Normalized name is pure whitespace and case folding, so this is an exact match and not a fuzzy
    one.  It is offered for human review; nothing in this change merges on it or joins by it.
    """

    entity_id: UUID
    display_name: str
    kind: CatalogEntityKind
    identity_status: CatalogEntityIdentityStatus
    event_count: int


@dataclass(frozen=True, slots=True)
class CatalogEntityGraph:
    """One bounded ego bundle: ring 0 the ego, ring 1 its events, ring 2 peers, ring 3 topics."""

    focus_id: str
    generated_at: datetime
    counts: CatalogEntityGraphCounts
    truncated: CatalogEntityGraphTruncation
    nodes: tuple[CatalogEntityGraphNode, ...]
    edges: tuple[CatalogEntityGraphEdge, ...]
    same_name_candidates: tuple[CatalogEntitySameNameCandidate, ...]


@dataclass(frozen=True, slots=True)
class CatalogEntityDirectoryTotals:
    """Whole-catalog entity composition, independent of the current filter."""

    entity_count: int
    person_count: int
    organization_count: int
    unknown_count: int
    verified_count: int
    scoped_count: int


@dataclass(frozen=True, slots=True)
class CatalogEntityDirectoryCoverage:
    """The honesty contract: how much of the catalog names anyone at all.

    Computed beside the ranking it qualifies so the disclosure cannot drift away from it.
    """

    events_with_entities: int
    events_total: int
    mention_count: int


@dataclass(frozen=True, slots=True)
class CatalogEntityHub:
    """One ranked directory row.  Ranked by measured event count, never by a composite score."""

    entity_id: UUID
    display_name: str
    kind: CatalogEntityKind
    identity_status: CatalogEntityIdentityStatus
    profile_url: str | None
    profile_key: str | None
    event_count: int
    upcoming_count: int
    peer_count: int
    source_count: int
    roles: tuple[str, ...]
    top_city: str | None
    last_event_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogEntityDirectory:
    """The ranked front door, with its coverage caveat and its unfiltered totals attached."""

    generated_at: datetime
    totals: CatalogEntityDirectoryTotals
    coverage: CatalogEntityDirectoryCoverage
    matched: int
    hubs: tuple[CatalogEntityHub, ...]
