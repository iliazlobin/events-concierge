"""Payload parsing for the ego graph and the ranked entity directory.

The two reads themselves live on :class:`~.catalog_entities.PostgresCatalogEntityRepository`,
beside the entity reads they belong with.  Both are **one** statement.  The graph in particular must
never route through :meth:`PostgresCatalogEntityRepository.get`, which issues five sequential
statements for a single entity: hydrating even 32 peers that way would be 160 round trips against a
pool configured ``pool_size=5, max_overflow=0``.  All hydration happens inside the security-definer
capability, and this module only parses the ``jsonb`` it returns.

Nothing here reaches a connection, which is what makes every shape below directly testable against a
payload captured from the live catalog.  The one vocabulary this module needs -- the entity kinds --
is derived from the domain ``Literal`` rather than restated, so a future migration that widens it
cannot leave a second Python copy behind rejecting valid input.

A payload that is missing a required key, or carries the wrong type in one, raises :class:`ValueError`
and never :class:`KeyError`: a capability revision is a shape change, and the caller that has to
decide between a 500 and a degraded render needs one exception class to catch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast, get_args
from uuid import UUID

from ...domain.catalog_entities import CatalogEntityIdentityStatus, CatalogEntityKind
from ...domain.catalog_entity_graph import (
    CatalogEntityDirectory,
    CatalogEntityDirectoryCoverage,
    CatalogEntityDirectoryTotals,
    CatalogEntityGraph,
    CatalogEntityGraphCounts,
    CatalogEntityGraphEdge,
    CatalogEntityGraphEdgeKind,
    CatalogEntityGraphNode,
    CatalogEntityGraphNodeKind,
    CatalogEntityGraphTruncation,
    CatalogEntityHub,
    CatalogEntitySameNameCandidate,
)

_ENTITY_KINDS = frozenset(get_args(CatalogEntityKind))
_IDENTITY_STATUSES = frozenset(get_args(CatalogEntityIdentityStatus))

# ``node_id`` is a render-time namespace, never a persisted key.  Parsing is a strict prefix match
# so a payload can never smuggle an unnamespaced value into an ``entity_id`` position.
_NODE_KIND_PREFIXES: dict[str, str] = {
    "entity": "entity:",
    "event": "event:",
    "topic": "topic:",
}
_EDGE_KINDS = frozenset(get_args(CatalogEntityGraphEdgeKind))


def graph_from_payload(payload: object) -> CatalogEntityGraph:
    """Parse ``fn_get_catalog_entity_graph_v1``'s ``jsonb`` document into domain values."""
    document = _mapping(payload)
    return CatalogEntityGraph(
        focus_id=_text(_require(document, "focus_id")),
        generated_at=_timestamp(_require(document, "generated_at")),
        counts=_counts(_require(document, "counts")),
        truncated=_truncation(_require(document, "truncated")),
        nodes=tuple(_node(entry) for entry in _sequence(_require(document, "nodes"))),
        edges=tuple(_edge(entry) for entry in _sequence(_require(document, "edges"))),
        same_name_candidates=tuple(
            _same_name_candidate(entry)
            for entry in _sequence(_require(document, "same_name_candidates"))
        ),
    )


def directory_from_payload(payload: object) -> CatalogEntityDirectory:
    """Parse ``fn_get_catalog_entity_directory_v1``'s ``jsonb`` document into domain values."""
    document = _mapping(payload)
    totals = _mapping(_require(document, "totals"))
    coverage = _mapping(_require(document, "coverage"))
    return CatalogEntityDirectory(
        generated_at=_timestamp(_require(document, "generated_at")),
        totals=CatalogEntityDirectoryTotals(
            entity_count=_int(_require(totals, "entity_count")),
            person_count=_int(_require(totals, "person_count")),
            organization_count=_int(_require(totals, "organization_count")),
            unknown_count=_int(_require(totals, "unknown_count")),
            verified_count=_int(_require(totals, "verified_count")),
            scoped_count=_int(_require(totals, "scoped_count")),
        ),
        coverage=CatalogEntityDirectoryCoverage(
            events_with_entities=_int(_require(coverage, "events_with_entities")),
            events_total=_int(_require(coverage, "events_total")),
            mention_count=_int(_require(coverage, "mention_count")),
        ),
        matched=_int(_require(document, "matched")),
        hubs=tuple(_hub(entry) for entry in _sequence(_require(document, "hubs"))),
    )


def _counts(value: object) -> CatalogEntityGraphCounts:
    """Parse the count block so ``edges`` always equals ``len(graph.edges)``.

    Two capability revisions are in the wild and they disagree about what ``counts.edges`` names.
    ``0165`` emits the emitted-array count plus a separate ``mention_edges``; the body ``0156``
    installed emits *only* the mention count under the name ``edges``, while still appending one
    topic edge per topic node to the array.  Taking ``edges`` verbatim from the older body
    understates the drawn graph by exactly the topic count.

    So the split is reconstructed rather than trusted: when ``mention_edges`` is absent, the number
    reported as ``edges`` is the mention count, and the emitted total is that plus the topics.  Both
    revisions then produce identical values, and :attr:`CatalogEntityGraphCounts.edges` keeps the
    promise its docstring makes.
    """
    counts = _mapping(value)
    topics = _int(_require(counts, "topics"))
    reported = _int(_require(counts, "edges"))
    mention_edges = counts.get("mention_edges")
    return CatalogEntityGraphCounts(
        events=_int(_require(counts, "events")),
        events_total=_int(_require(counts, "events_total")),
        peers=_int(_require(counts, "peers")),
        peers_total=_int(_require(counts, "peers_total")),
        topics=topics,
        edges=reported if mention_edges is not None else reported + topics,
        mention_edges=reported if mention_edges is None else _int(mention_edges),
        edges_total=_int(_require(counts, "edges_total")),
    )


def _truncation(value: object) -> CatalogEntityGraphTruncation:
    truncated = _mapping(value)
    return CatalogEntityGraphTruncation(
        events=_bool(_require(truncated, "events")),
        peers=_bool(_require(truncated, "peers")),
        edges=_bool(_require(truncated, "edges")),
    )


def _node(value: object) -> CatalogEntityGraphNode:
    node = _mapping(value)
    node_id = _text(_require(node, "node_id"))
    node_kind = _node_kind(_require(node, "node_kind"), node_id)
    return CatalogEntityGraphNode(
        node_id=node_id,
        node_kind=node_kind,
        ring=_int(_require(node, "ring")),
        label=_text(_require(node, "label")),
        # An entity absent from the degree aggregate has no recorded mention, and zero distinct
        # events is exactly what that means.
        degree=_int(node.get("degree"), default=0),
        entity_id=_optional_uuid(node.get("entity_id")),
        entity_kind=_optional_entity_kind(node.get("entity_kind")),
        identity_status=_optional_identity_status(node.get("identity_status")),
        profile_url=_optional_text(node.get("profile_url")),
        profile_key=_optional_text(node.get("profile_key")),
        canonical_event_id=_optional_uuid(node.get("canonical_event_id")),
        start_at=_optional_timestamp(node.get("start_at")),
        end_at=_optional_timestamp(node.get("end_at")),
        is_past=None if node.get("is_past") is None else _bool(node["is_past"]),
        venue_name=_optional_text(node.get("venue_name")),
        city=_optional_text(node.get("city")),
        price_status=_optional_text(node.get("price_status")),
        topics=_text_tuple(node.get("topics")),
        ego_roles=_text_tuple(node.get("ego_roles")),
        registration_url=_optional_text(node.get("registration_url")),
        shared_event_count=_optional_int(node.get("shared_event_count")),
        roles=_text_tuple(node.get("roles")),
    )


def _edge(value: object) -> CatalogEntityGraphEdge:
    edge = _mapping(value)
    kind = _text(_require(edge, "kind"))
    if kind not in _EDGE_KINDS:
        raise ValueError(f"unknown catalog entity graph edge kind: {kind!r}")
    return CatalogEntityGraphEdge(
        a=_namespaced_id(_require(edge, "a")),
        b=_namespaced_id(_require(edge, "b")),
        kind=cast(CatalogEntityGraphEdgeKind, kind),
        roles=_text_tuple(edge.get("roles")),
        source_labels=_text_tuple(edge.get("source_labels")),
        observed_at=_optional_timestamp(edge.get("observed_at")),
    )


def _same_name_candidate(value: object) -> CatalogEntitySameNameCandidate:
    candidate = _mapping(value)
    return CatalogEntitySameNameCandidate(
        entity_id=_uuid(_require(candidate, "entity_id")),
        display_name=_text(_require(candidate, "display_name")),
        kind=_entity_kind(_require(candidate, "kind")),
        identity_status=_identity_status(_require(candidate, "identity_status")),
        event_count=_int(_require(candidate, "event_count")),
    )


def _hub(value: object) -> CatalogEntityHub:
    hub = _mapping(value)
    return CatalogEntityHub(
        entity_id=_uuid(_require(hub, "entity_id")),
        display_name=_text(_require(hub, "display_name")),
        kind=_entity_kind(_require(hub, "kind")),
        identity_status=_identity_status(_require(hub, "identity_status")),
        profile_url=_optional_text(hub.get("profile_url")),
        profile_key=_optional_text(hub.get("profile_key")),
        event_count=_int(_require(hub, "event_count")),
        upcoming_count=_int(_require(hub, "upcoming_count")),
        peer_count=_int(hub.get("peer_count"), default=0),
        source_count=_int(_require(hub, "source_count")),
        roles=_text_tuple(hub.get("roles")),
        top_city=_optional_text(hub.get("top_city")),
        last_event_at=_optional_timestamp(hub.get("last_event_at")),
    )


def _node_kind(value: object, node_id: str) -> CatalogEntityGraphNodeKind:
    kind = _text(value)
    prefix = _NODE_KIND_PREFIXES.get(kind)
    if prefix is None:
        raise ValueError(f"unknown catalog entity graph node kind: {kind!r}")
    if not node_id.startswith(prefix):
        raise ValueError(f"catalog entity graph node id {node_id!r} does not match kind {kind!r}")
    return cast(CatalogEntityGraphNodeKind, kind)


def _namespaced_id(value: object) -> str:
    identifier = _text(value)
    if not any(identifier.startswith(prefix) for prefix in _NODE_KIND_PREFIXES.values()):
        raise ValueError(f"catalog entity graph node id is not namespaced: {identifier!r}")
    return identifier


def _entity_kind(value: object) -> CatalogEntityKind:
    kind = _text(value)
    if kind not in _ENTITY_KINDS:
        raise ValueError(f"unknown catalog entity kind: {kind!r}")
    return cast(CatalogEntityKind, kind)


def _optional_entity_kind(value: object) -> CatalogEntityKind | None:
    return None if value is None else _entity_kind(value)


def _identity_status(value: object) -> CatalogEntityIdentityStatus:
    status = _text(value)
    if status not in _IDENTITY_STATUSES:
        raise ValueError(f"unknown catalog entity identity status: {status!r}")
    return cast(CatalogEntityIdentityStatus, status)


def _optional_identity_status(value: object) -> CatalogEntityIdentityStatus | None:
    return None if value is None else _identity_status(value)


def _require(mapping: Mapping[str, Any], key: str) -> object:
    """Read a key the payload contract requires, as a ``ValueError`` rather than a ``KeyError``."""
    if key not in mapping:
        raise ValueError(f"catalog entity graph payload is missing {key!r}")
    return mapping[key]


def _mapping(value: object) -> Mapping[str, Any]:
    """Accept the driver's decoded object, or the raw text a plain client would hand back."""
    decoded = json.loads(value) if isinstance(value, str | bytes) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("catalog entity graph payload is not an object")
    return cast(Mapping[str, Any], decoded)


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError("catalog entity graph payload member is not an array")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"catalog entity graph payload member is not text: {value!r}")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _int(value: object, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"catalog entity graph payload member is not an integer: {value!r}")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"catalog entity graph payload member is not a boolean: {value!r}")
    return value


def _uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(_text(value))


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else _uuid(value)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(_text(value))


def _optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _timestamp(value)


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_text(entry) for entry in _sequence(value))
