"""Exercise runtime release profiles in the built Next.js consumer, using isolated API fixtures."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect
from tests.e2e.test_consumer_ui import _TRANSPARENT_MAP_TILE, _feed

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL is not set"),
]

ENTITY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HOST_ENTITY_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ISOLATED_ENTITY_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SECOND_EVENT_ID = "33333333-3333-4333-8333-333333333333"


def catalog_event():
    return _feed()["items"][0] | {
        "organizer_name": "Lakehouse Music", "topics": ["jazz"],
        "host_names": ["Alex Example"], "registration_status": "open",
        "entity_profiles": [{"name": "Alex Example", "role": "host", "kind": "person",
                             "profile_url": "https://www.linkedin.com/in/alex-example"}],
        "source_keys": ["fixture-jazz"], "providers": ["Fixture Jazz"],
        "sources": [{"source_key": "fixture-jazz", "source": "public_jsonld", "label": "Fixture Jazz", "registration_url": "https://events.example.test/friday-jazz"}],
    }


def second_catalog_event(recurring: bool = False):
    return catalog_event() | {
        "canonical_event_id": SECOND_EVENT_ID,
        "title": "Friday Night Jazz" if recurring else "Saturday Night Jazz",
        "start_at": "2030-06-15T19:00:00-07:00",
        "end_at": "2030-06-15T21:00:00-07:00",
        "description": "Saturday's own event details.",
    }


def entity_graph(entity_id: str = ENTITY_ID):
    event = _feed()["items"][0]
    is_host = entity_id == HOST_ENTITY_ID
    role = "host" if is_host else "organizer"
    entity_node = f"entity:{entity_id}"
    event_node = f"event:{event['canonical_event_id']}"
    return {
        "focus_id": entity_node, "generated_at": "2030-06-01T00:00:00Z",
        "counts": {"events": 1, "events_total": 1, "peers": 0, "peers_total": 0,
                   "topics": 0, "edges": 1, "mention_edges": 1, "edges_total": 1},
        "truncated": {"events": False, "peers": False, "edges": False},
        "same_name_candidates": [],
        "nodes": [
            {"node_id": entity_node, "node_kind": "entity", "ring": 0,
             "label": "Alex Example" if is_host else "Lakehouse Music", "entity_id": entity_id,
             "entity_kind": "person" if is_host else "organization", "identity_status": "profile_verified",
             "profile_url": "https://www.linkedin.com/in/alex-example" if is_host else "https://events.example.test/lakehouse", "degree": 1,
             "roles": [role]},
            {"node_id": event_node, "node_kind": "event", "ring": 1,
             "label": event["title"], "canonical_event_id": event["canonical_event_id"],
             "start_at": event["start_at"], "end_at": event["end_at"], "is_past": False,
             "venue_name": "Lakehouse", "city": "Oakland", "price_status": "free",
             "topics": ["jazz"], "ego_roles": [role], "degree": 1,
             "registration_url": event["registration_urls"][0]},
        ],
        "edges": [{"a": entity_node, "b": event_node, "kind": "mention",
                   "roles": [role], "source_labels": ["Fixture Jazz"],
                   "observed_at": "2030-06-01T00:00:00Z"}],
    }


def multi_hub_overview_graph():
    """Representative catalog frame: two connected hubs and one isolated hub."""
    organizer = entity_graph()
    host = entity_graph(HOST_ENTITY_ID)
    return organizer | {
        "focus_id": "catalog:overview",
        "counts": organizer["counts"] | {
            "peers": 3, "peers_total": 3, "edges": 2,
            "mention_edges": 2, "edges_total": 2,
        },
        "nodes": [
            organizer["nodes"][0] | {"degree": 12},
            host["nodes"][0] | {"degree": 5},
            {"node_id": f"entity:{ISOLATED_ENTITY_ID}", "node_kind": "entity", "ring": 0,
             "label": "Quiet Society", "entity_id": ISOLATED_ENTITY_ID,
             "entity_kind": "organization", "identity_status": "source_scoped",
             "degree": 3, "roles": ["organizer"]},
            organizer["nodes"][1] | {"degree": 2, "shared_event_count": 4},
        ],
        "edges": [
            organizer["edges"][0] | {"source_labels": ["Fixture Organizer Calendar"]},
            host["edges"][0] | {"source_labels": ["Fixture Host Notes"],
                                "observed_at": "2030-06-02T00:00:00Z"},
        ],
    }


def connected_entity_graph(entity_id: str):
    """The same shared event in a bounded entity neighborhood, with role-specific evidence."""
    graph = entity_graph(entity_id)
    is_host = entity_id == HOST_ENTITY_ID
    peer = entity_graph(ENTITY_ID if is_host else HOST_ENTITY_ID)
    return graph | {
        "counts": graph["counts"] | {
            "events_total": 5 if is_host else 12, "peers": 1, "peers_total": 1,
            "edges": 2, "mention_edges": 2, "edges_total": 2,
        },
        "truncated": graph["truncated"] | {"events": True},
        "nodes": [
            graph["nodes"][0] | {"degree": 5 if is_host else 12},
            peer["nodes"][0] | {"ring": 2, "degree": 12 if is_host else 5, "shared_event_count": 4},
            graph["nodes"][1] | {"degree": 2},
        ],
        "edges": [
            graph["edges"][0] | {"source_labels": ["Fixture Host Notes" if is_host else "Fixture Organizer Calendar"]},
            peer["edges"][0] | {"source_labels": ["Fixture Organizer Calendar" if is_host else "Fixture Host Notes"]},
        ],
    }


@dataclass
class ReleaseApi:
    profile: str | None = "discovery"
    local_demo: bool = True
    config_status: int = 200
    hold_config: bool = False
    held: list[Route] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    entity_resolutions: list[dict[str, list[str]]] = field(default_factory=list)
    resolution_status: int = 200
    unexpected: list[str] = field(default_factory=list)
    graph_status: int = 200
    empty_graph: bool = False
    social_profiles: bool = False
    event_status: int = 200
    hold_events: bool = False
    held_events: list[Route] = field(default_factory=list)
    second_event: bool = False
    recurring: bool = False
    social_links_only: bool = False
    event_url: str | None = "https://events.example.test/friday-jazz"
    multi_hub_overview: bool = False
    entity_refresh_due: bool = True
    hold_entity_details: bool = False
    held_entity_details: list[tuple[Route, dict]] = field(default_factory=list)

    def with_event_url(self, event):
        return event | {"registration_urls": [self.event_url] if self.event_url else [],
                        "sources": [source | {"registration_url": self.event_url} for source in event["sources"]]}

    def config(self, route: Route) -> None:
        payload = {
            "product_name": "Events Concierge",
            "local_demo": self.local_demo,
            "auth_mode": "local_demo" if self.local_demo else "deployment_session",
            "auth_start_url": None,
            "reauth_url": None,
            "logout_url": None,
            "csrf_cookie_name": None,
            "csrf_header_name": None,
        }
        if self.profile is not None:
            payload["release_profile"] = self.profile
        self.respond(route, payload if self.config_status == 200 else {"detail": "Config unavailable"}, self.config_status)

    @staticmethod
    def respond(route: Route, payload, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def install(self, page: Page) -> None:
        page.route("**/v1/**", self.handle)
        page.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(content_type="image/png", body=_TRANSPARENT_MAP_TILE))

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        self.calls.append((route.request.method, path))
        if path == "/v1/ui-config":
            if self.hold_config:
                self.held.append(route)
            else:
                self.config(route)
        elif path == "/v1/me":
            self.respond(route, {
                "notify_email": "browser@example.test", "interests": [],
                "preference_revision": 1, "local_demo": True, "is_admin": False,
                "relay_inbox": "fixture@relay.example.test",
            })
        elif path in {"/v1/me/saved-filters", "/v1/me/api-keys"}:
            self.respond(route, [])
        elif path == "/v1/catalog/events":
            event = self.with_event_url(catalog_event())
            self.respond(route, {
                "items": [event], "next_cursor": None,
                "providers": [{"source_key": "fixture-jazz", "label": "Fixture Jazz", "display_name": "Fixture Jazz", "publisher": "Fixture Jazz", "provider": "public_jsonld", "seed_url": "https://events.example.test", "event_count": 1}],
                "topic_facets": [{"topic": "jazz", "label": "Jazz", "event_count": 1}],
                "city_facets": [{"city": "Oakland", "event_count": 1}],
            })
        elif path == "/v1/catalog/events/summary":
            self.respond(route, {"days": [{"start_day": "2030-06-14", "event_count": 1, "topics": [{"topic": "jazz", "label": "Jazz", "event_count": 1}]}], "total_event_count": 1, "time_zone": "America/Los_Angeles"})
        elif path.startswith("/v1/catalog/events/"):
            self.handle_event(route, path)
        elif path.startswith(("/v1/catalog/entity", "/v1/catalog/entities/")):
            self.handle_entity(route, path)
        else:
            self.unexpected.append(path)
            self.respond(route, {"detail": f"Unexpected fixture request: {path}"}, 500)


    def handle_event(self, route: Route, path: str) -> None:
        if self.hold_events:
            self.held_events.append(route)
            return
        event = catalog_event()
        if path.endswith(SECOND_EVENT_ID):
            event = second_catalog_event(self.recurring)
        self.respond(route, self.with_event_url(event) if self.event_status == 200 else {"detail": "Event unavailable"}, self.event_status)

    def handle_entity(self, route: Route, path: str) -> None:
        if path == "/v1/catalog/entity-resolution":
            query = parse_qs(urlsplit(route.request.url).query)
            self.entity_resolutions.append(query)
            if self.resolution_status != 200:
                self.respond(route, {"detail": "Entity resolution unavailable"}, self.resolution_status)
                return
            identities = {("host", "Alex Example"): HOST_ENTITY_ID,
                          ("organizer", "Lakehouse Music"): ENTITY_ID}
            entity_id = identities.get((query.get("role", [None])[0], query.get("name", [None])[0]))
            event_id = query.get("canonical_event_id", [None])[0]
            if entity_id and event_id in {catalog_event()["canonical_event_id"], SECOND_EVENT_ID}:
                self.respond(route, {"entity_id": entity_id})
            else:
                self.respond(route, {"detail": "Entity assertion not found"}, 404)
        elif path in {"/v1/catalog/entity-overview-graph", f"/v1/catalog/entities/{ENTITY_ID}/graph",
                       f"/v1/catalog/entities/{HOST_ENTITY_ID}/graph"}:
            self.handle_entity_graph(route, path)
        elif path == "/v1/catalog/entity-directory":
            self.respond(route, {
                "generated_at": "2030-06-01T00:00:00Z", "matched": 3 if self.multi_hub_overview else 1, "hubs": [],
                "totals": {"entity_count": 3 if self.multi_hub_overview else 1,
                           "person_count": int(self.multi_hub_overview),
                           "organization_count": 2 if self.multi_hub_overview else 1,
                           "unknown_count": 0, "verified_count": 2 if self.multi_hub_overview else 1,
                           "scoped_count": int(self.multi_hub_overview)},
                "coverage": {"events_with_entities": 1, "events_total": 1,
                             "mention_count": 2 if self.multi_hub_overview else 1},
            })
        elif path in {f"/v1/catalog/entities/{ENTITY_ID}", f"/v1/catalog/entities/{HOST_ENTITY_ID}"}:
            entity_id = HOST_ENTITY_ID if path.endswith(HOST_ENTITY_ID) else ENTITY_ID
            identity = entity_graph(entity_id)["nodes"][0]
            is_host = entity_id == HOST_ENTITY_ID
            payload = {
                "entity": {"entity_id": entity_id, "display_name": identity["label"],
                           "kind": identity["entity_kind"], "identity_status": "profile_verified",
                           "canonical_profile_url": identity["profile_url"],
                           "summary": None, "website_url": None, "logo_url": None,
                           "city": "Oakland", "country": "US", "event_count": 1,
                           "roles": identity["roles"], "source_count": 1, "research_status": "researchable"},
                "events": [], "external_sources": (_social_sources() if self.social_profiles and not is_host else [])
                + (_linked_social_sources() if (self.social_profiles or self.social_links_only) and not is_host else []),
                "external_facts": [{"provider_key": "website",
                                    "source_url": identity["profile_url"],
                                    "fact_key": "description", "value": "Published host profile fixture." if is_host else "Published profile fixture.",
                                    "value_url": None, "sort_order": 0,
                                    "observed_at": "2030-06-01T00:00:00Z"}] + (_social_facts() if self.social_profiles and not is_host else []),
                "refresh_due": self.entity_refresh_due, "insights": None,
            }
            if self.hold_entity_details:
                self.held_entity_details.append((route, payload))
            else:
                self.respond(route, payload)
        else:
            self.unexpected.append(path)
            self.respond(route, {"detail": "Unknown entity fixture"}, 500)

    def handle_entity_graph(self, route: Route, path: str) -> None:
        graph = entity_graph(HOST_ENTITY_ID if HOST_ENTITY_ID in path else ENTITY_ID)
        if path.endswith("entity-overview-graph"):
            graph["focus_id"] = "catalog:overview"
            graph["counts"]["peers"] = 1
            graph["counts"]["peers_total"] = 1
            if self.multi_hub_overview:
                graph = multi_hub_overview_graph()
        elif self.multi_hub_overview:
            graph = connected_entity_graph(HOST_ENTITY_ID if HOST_ENTITY_ID in path else ENTITY_ID)
        if self.second_event:
            second = second_catalog_event(self.recurring)
            node = graph["nodes"][-1] | {"node_id": f"event:{SECOND_EVENT_ID}",
                    "canonical_event_id": SECOND_EVENT_ID, "label": second["title"],
                    "start_at": second["start_at"], "end_at": second["end_at"]}
            graph["nodes"].append(node)
            graph["edges"].append(graph["edges"][0] | {"b": node["node_id"]})
            graph["counts"].update(events=2, events_total=2, edges=2, mention_edges=2, edges_total=2)
        if self.empty_graph:
            graph["nodes"] = []
            graph["edges"] = []
            graph["counts"] = dict.fromkeys(graph["counts"], 0)
        self.respond(route, graph if self.graph_status == 200 else {"detail": "Graph unavailable"}, self.graph_status)


def _social_sources():
    return [
        {"provider_key": key, "external_id": "12345", "source_url": url, "display_name": label,
         "status": status, "fetched_at": "2030-06-01T00:00:00Z",
         "next_refresh_at": "2030-06-02T00:00:00Z", "error_code": "unavailable" if status=="failed" else None}
        for key,url,label,status in [
            ("x_public_api","https://x.com/social_builder","X API","failed"),
            ("instagram_public_api","https://www.instagram.com/social_builder","Instagram API","fresh")]
    ]


def _linked_social_sources():
    return [
        {"provider_key": key, "external_id": "social_builder", "source_url": url,
         "display_name": label, "status": "linked", "fetched_at": None,
         "next_refresh_at": "2030-06-02T00:00:00Z", "error_code": None}
        for key, url, label in [
            ("x_profile", "https://x.com/social_builder", "X"),
            ("instagram_profile", "https://www.instagram.com/social_builder", "Instagram"),
        ]
    ]


def _social_facts():
    return [
        {"provider_key": key,"source_url": "https://x.com/social_builder", "fact_key": fact,
         "value": value,"value_url": url,"sort_order":0,"observed_at":"2030-06-01T00:00:00Z"}
        for key,fact,value,url in [
            ("x_public_api","description","Public AI community",None),
            ("x_public_api","followers","6412",None),
            ("x_public_api","avatar","Profile image","https://pbs.twimg.com/avatar.png"),
            ("instagram_public_api","description","Community gatherings",None),
            ("instagram_public_api","followers","4300",None)]
    ]


@pytest.fixture
def release_page(page_factory):
    harness = page_factory(authenticated=True)
    api = ReleaseApi()
    api.install(harness.page)
    yield harness, api
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)


def test_discovery_rejects_chat_deep_links_and_history_but_keeps_provider_actions(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view=chat&entity=hidden&release_profile=full")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)
    expect(page.locator(".site-nav").get_by_role("button", name="Entities", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["view"] == ["events"]
    assert "entity" not in parse_qs(urlsplit(page.url).query)
    page.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    expect(page.get_by_role("button", name="Explore organizer Lakehouse Music", exact=True)).to_be_visible()
    expect(page.locator('a[href="https://events.example.test/friday-jazz"]')).to_be_visible()
    expect(page.get_by_role("link", name=re.compile(r"^Add Friday Night Jazz .* to Google Calendar$"))).to_be_visible()
    page.get_by_role("button", name="Add jazz topic filter", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]topic=jazz(?:&|$)"))
    assert parse_qs(urlsplit(page.url).query)["view"] == ["entities"]
    page.evaluate("""() => {
        const state = structuredClone(history.state);
        state.eventsConciergeConsumer.view = 'chat';
        state.eventsConciergeConsumer.selectedEntityId = 'hidden';
        history.pushState(state, '', '?view=chat');
        dispatchEvent(new PopStateEvent('popstate', {state}));
    }""")
    expect(page).to_have_url(re.compile(r"[?&]view=events(?:&|$)"))
    expect(page.locator(".chat-view")).to_have_count(0)
    page.get_by_role("button", name="Events Concierge", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]view=events(?:&|$)"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".mobile-nav button")).to_have_count(4)
    assert all("agent" not in path and "entities" not in path for _, path in api.calls)


def test_discovery_entity_graph_and_profile_are_read_only(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    expect(page.get_by_role("heading", name="Lakehouse Music", exact=True).first).to_be_visible()
    page.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(page.get_by_role("heading", name="Profiles", exact=True)).to_be_visible()
    expect(page.get_by_text("Published profile fixture.", exact=True)).to_be_visible()
    assert ("GET", f"/v1/catalog/entities/{ENTITY_ID}") in api.calls
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)
    assert not any(path.endswith("/refresh") for _, path in api.calls)
    expect(page.get_by_role("application")).to_be_visible()
    expect(page.get_by_role("group", name="Graph or text", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Text", exact=True)).to_have_count(0)
    page.locator(".site-nav").get_by_role("button", name="Events", exact=True).click()
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Lakehouse Music", exact=True).first).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["entity"] == [ENTITY_ID]
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".mobile-nav button")).to_have_count(4)
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)
    expect(page.get_by_role("application")).to_be_visible()
    expect(page.get_by_role("button", name="Show graph", exact=True)).to_have_count(0)


def test_discovery_entities_overview_resolves_an_event_organizer(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view=entities")
    expect(page.get_by_role("heading", name="Graph", exact=True)).to_be_visible()
    expect(page.locator(f'button[data-node-id="entity:{ENTITY_ID}"]')).to_be_visible()
    assert ("GET", "/v1/catalog/entity-overview-graph") in api.calls
    assert ("GET", "/v1/catalog/entity-directory") in api.calls
    page.locator(".site-nav").get_by_role("button", name="Events", exact=True).click()
    page.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    page.get_by_role("button", name="Explore organizer Lakehouse Music", exact=True).click()
    expect(page.get_by_role("heading", name="Lakehouse Music", exact=True).first).to_be_visible()
    assert ("GET", "/v1/catalog/entity-resolution") in api.calls


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("role,entity_id,name,profile,description", [
    ("host", HOST_ENTITY_ID, "Alex Example", "https://www.linkedin.com/in/alex-example", "Published host profile fixture."),
    ("organizer", ENTITY_ID, "Lakehouse Music", "https://events.example.test/lakehouse", "Published profile fixture."),
])
def test_graph_event_participant_profiles_load_once_without_writes(release_page, width, role, entity_id, name, profile, description):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities")
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    page.get_by_role("button", name=f"Explore {role} {name}", exact=True).click()
    expect(page.get_by_role("heading", name=name, exact=True).first).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["entity"] == [entity_id]
    detail_path = f"/v1/catalog/entities/{entity_id}"
    assert ("GET", detail_path) not in api.calls
    page.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(page.get_by_text(description, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Profiles", exact=True)).to_be_visible()
    expect(page.locator(f'.entity-graph-inspector a[href="{profile}"]').first).to_be_visible()
    expect(page.locator("main").get_by_role("alert")).to_have_count(0)
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(page.get_by_text(description, exact=True)).to_be_visible()
    assert api.calls.count(("GET", detail_path)) == 1
    assert not any(path.endswith("/refresh") for _, path in api.calls)


@pytest.mark.parametrize("width", [1440, 390])
def test_graph_topic_inspector_counts_all_drawn_events(release_page, width):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    graph = entity_graph()
    second = second_catalog_event()
    graph["nodes"].extend([
        graph["nodes"][1] | {"node_id": f"event:{SECOND_EVENT_ID}",
                               "canonical_event_id": SECOND_EVENT_ID,
                               "label": second["title"], "start_at": second["start_at"]},
        {"node_id": "topic:jazz", "node_kind": "topic", "ring": 3,
         "label": "jazz", "degree": 2},
    ])
    graph["edges"].append({"a": graph["focus_id"], "b": "topic:jazz", "kind": "topic",
                           "roles": [], "source_labels": [], "observed_at": None})
    graph["counts"] |= {"events": 2, "events_total": 2, "topics": 1, "edges": 2}
    graph["truncated"]["edges"] = True
    page.route(f"**/v1/catalog/entities/{ENTITY_ID}/graph*",
               lambda route: api.respond(route, graph))
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    page.locator('button[data-node-id="topic:jazz"]').click()
    inspector = page.get_by_role("complementary", name="Topic detail", exact=True)
    expect(inspector.get_by_role("heading", name="Jazz", exact=True)).to_be_visible()
    expect(inspector.get_by_text("2 of the 2 shown events", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_graph_workspace_breadcrumbs_and_browser_history(release_page, width):
    harness, _ = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities&topic=jazz")
    path = page.get_by_role("navigation", name="Graph navigation", exact=True)
    expect(path.get_by_role("heading", name="Jazz", exact=True)).to_be_visible()
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    page.get_by_role("button", name="Explore organizer Lakehouse Music", exact=True).click()
    expect(path.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Back", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="All entities", exact=True)).to_have_count(0)
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    page.get_by_role("button", name="Explore host Alex Example", exact=True).click()
    expect(path.get_by_role("heading", name="Alex Example", exact=True)).to_be_visible()
    page.go_back()
    expect(path.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    page.go_forward()
    expect(path.get_by_role("heading", name="Alex Example", exact=True)).to_be_visible()
    path.get_by_role("button", name="Graph", exact=True).press("Enter")
    expect(page.get_by_role("heading", name="Graph", exact=True)).to_be_visible()
    assert not {"entity", "topic"}.intersection(parse_qs(urlsplit(page.url).query))
    expect(page.get_by_role("navigation", name="Graph navigation", exact=True)).to_have_count(0)
    page.go_back()
    expect(path.get_by_role("heading", name="Alex Example", exact=True)).to_be_visible()
    page.go_back()
    expect(path.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    page.go_back()
    expect(path.get_by_role("heading", name="Jazz", exact=True)).to_be_visible()
    path.get_by_role("button", name="Graph", exact=True).click()
    expect(page.get_by_role("heading", name="Graph", exact=True)).to_be_visible()
    assert not {"entity", "topic"}.intersection(parse_qs(urlsplit(page.url).query))


@pytest.mark.parametrize("width", [1440, 390])
def test_graph_workspace_search_and_optional_filters(release_page, width):
    harness, _ = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities")
    expect(page.get_by_role("heading", name="Graph", exact=True)).to_be_visible()
    graph = page.get_by_role("application")
    expect(graph).to_be_visible()
    expect(page.get_by_role("heading", name="Entity explorer", exact=True)).to_have_count(0)
    assert graph.bounding_box()["y"] < 260
    expect(page.get_by_role("button", name=re.compile(r"^Organizations"))).not_to_be_visible()
    page.locator(".graph-workspace-filters summary").press("Enter")
    expect(page.get_by_text("In the whole catalog", exact=True)).to_be_visible()
    with page.expect_request(re.compile(r"entity-overview-graph.*kind=organization")):
        page.get_by_role("button", name=re.compile(r"^Organizations")).click()
    with page.expect_request(re.compile(r"entity-overview-graph.*identity=source_scoped")):
        page.get_by_role("button", name=re.compile(r"^Source-scoped")).click()
    expect(page.locator(".graph-workspace-filters summary")).to_have_text("Filters2")
    page.locator(".graph-workspace-filters summary").press("Enter")
    with page.expect_request(re.compile(r"entity-overview-graph.*q=Lakehouse")):
        page.get_by_role("textbox", name="Search people and organizations", exact=True).fill("Lakehouse")
    expect(graph).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("entity_id,name,provenance", [
    (ENTITY_ID, "Lakehouse Music", "Organizer · via Fixture Organizer Calendar"),
    (HOST_ENTITY_ID, "Alex Example", "Host · via Fixture Host Notes"),
    (ISOLATED_ENTITY_ID, "Quiet Society", None),
])
def test_catalog_graph_entity_selection_uses_the_full_inspector(release_page, width, entity_id, name, provenance):
    harness, api = release_page
    api.multi_hub_overview = True
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities")
    expect(page.get_by_role("application")).to_be_visible()
    expect(page.get_by_role("complementary", name="Entity detail", exact=True)).to_have_count(0)
    node = page.locator(f'button[data-node-id="entity:{entity_id}"]')
    if width == 1440:
        node.press("Enter")
    else:
        node.click()
    inspector = page.get_by_role("complementary", name="Entity detail", exact=True)
    expect(inspector.get_by_role("heading", name=name, exact=True)).to_be_visible()
    expect(inspector.get_by_role("tab", name="Overview", exact=True)).to_be_visible()
    expect(inspector.get_by_role("tab", name="Profile & sources", exact=True)).to_be_visible()
    expect(page.locator(".entity-overview-selection")).to_have_count(0)
    expect(page.get_by_role("button", name="Open this graph", exact=True)).to_have_count(0)
    expect(inspector.get_by_text(re.compile(r"\b\d+ upcoming\b"))).to_have_count(0)
    inspector.get_by_role("tab", name="Appearances", exact=True).click()
    expect(inspector.get_by_text(re.compile("representative|sampled", re.I))).to_be_visible()
    if provenance:
        appearance = inspector.locator(".entity-graph-inspector__appearances button")
        expect(appearance).to_have_count(1)
        expect(appearance).to_contain_text("Friday Night Jazz")
        expect(appearance.locator("small")).to_have_text(provenance)
        appearance.click()
        event = page.get_by_role("complementary", name="Event detail", exact=True)
        expect(event.locator(".event-card__title")).to_have_text("Friday Night Jazz")
        expect(inspector).to_have_count(0)
        event.get_by_role("button", name=re.compile("^(Close details|Back to the graph)$")).click()
    else:
        expect(inspector.get_by_text("No appearances in this frame.", exact=True)).to_be_visible()
        inspector.get_by_role("button", name="Close details", exact=True).click()
    expect(page.get_by_role("complementary", name=re.compile("^(Entity|Event) detail$"))).to_have_count(0)
    node.click()
    expect(inspector.get_by_role("heading", name=name, exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(inspector).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query).get("entity") is None
    assert not any(path.endswith("/graph") or path in {
        f"/v1/catalog/entities/{ENTITY_ID}", f"/v1/catalog/entities/{HOST_ENTITY_ID}",
        f"/v1/catalog/entities/{ISOLATED_ENTITY_ID}",
    } for _, path in api.calls)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_catalog_graph_first_hub_focus_gestures_use_browser_history(release_page, width):
    harness, api = release_page
    api.multi_hub_overview = True
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities")
    # The first ring-0 hub must not be mistaken for the overview's ego.
    first = page.locator(f'button[data-node-id="entity:{ENTITY_ID}"]')
    if width == 1440:
        first.press("Shift+Enter")
    else:
        first.dblclick()
    expect(page.get_by_role("navigation", name="Graph navigation", exact=True)
           .get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["entity"] == [ENTITY_ID]
    assert ("GET", f"/v1/catalog/entities/{ENTITY_ID}/graph") in api.calls
    page.locator(f'button[data-node-id="entity:{HOST_ENTITY_ID}"]').click()
    inspector = page.get_by_role("complementary", name="Entity detail", exact=True)
    expect(inspector.get_by_role("heading", name="Alex Example", exact=True)).to_be_visible()
    inspector.get_by_role("tab", name="Appearances", exact=True).click()
    appearance = inspector.locator(".entity-graph-inspector__appearances button")
    expect(appearance.locator("small")).to_have_text("Host · via Fixture Host Notes")
    appearance.click()
    event = page.get_by_role("complementary", name="Event detail", exact=True)
    expect(event.locator(".event-card__title")).to_have_text("Friday Night Jazz")
    event.get_by_role("button", name="Back to Lakehouse Music", exact=True).click()
    expect(inspector.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["entity"] == [ENTITY_ID]
    assert ("GET", f"/v1/catalog/entities/{HOST_ENTITY_ID}/graph") not in api.calls
    assert ("GET", f"/v1/catalog/entities/{HOST_ENTITY_ID}") not in api.calls
    page.go_back()
    expect(page.get_by_role("heading", name="Graph", exact=True)).to_be_visible()
    assert "entity" not in parse_qs(urlsplit(page.url).query)
    page.go_forward()
    expect(page.get_by_role("navigation", name="Graph navigation", exact=True)
           .get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()


def test_catalog_graph_profile_reads_follow_the_selected_entity_and_cache_late_results(release_page):
    harness, api = release_page
    api.multi_hub_overview = True
    api.hold_entity_details = True
    page = harness.page
    page.goto(f"{BASE}/?view=entities")
    host = page.locator(f'button[data-node-id="entity:{HOST_ENTITY_ID}"]')
    organizer = page.locator(f'button[data-node-id="entity:{ENTITY_ID}"]')
    host.click()
    inspector = page.get_by_role("complementary", name="Entity detail", exact=True)
    host_detail = f"/v1/catalog/entities/{HOST_ENTITY_ID}"
    assert ("GET", host_detail) not in api.calls
    with page.expect_request(f"**{host_detail}"):
        inspector.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(inspector.get_by_text("Loading profiles", exact=True)).to_be_visible()
    assert len(api.held_entity_details) == 1
    organizer.click()
    expect(inspector.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    expect(inspector.get_by_role("tab", name="Overview", exact=True)).to_have_attribute("aria-selected", "true")
    organizer_detail = f"/v1/catalog/entities/{ENTITY_ID}"
    assert ("GET", organizer_detail) not in api.calls
    api.hold_entity_details = False
    inspector.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(inspector.get_by_text("Published profile fixture.", exact=True)).to_be_visible()
    api.respond(*api.held_entity_details[0])
    expect(inspector.get_by_text("Published host profile fixture.", exact=True)).to_have_count(0)
    expect(inspector.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    host.click()
    inspector.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(inspector.get_by_text("Published host profile fixture.", exact=True)).to_be_visible()
    assert api.calls.count(("GET", host_detail)) == 1
    assert api.calls.count(("GET", organizer_detail)) == 1
    assert not any(path.endswith("/refresh") or path.endswith("/graph") for _, path in api.calls)


def test_full_catalog_graph_late_refresh_stays_with_its_requested_entity(page_factory):
    harness = page_factory(authenticated=True)
    page = harness.page
    api = ReleaseApi(profile="full", multi_hub_overview=True, entity_refresh_due=False)
    api.install(page)
    refresh_calls: list[tuple[str, str]] = []
    held_refreshes: list[Route] = []

    def hold_refresh(route: Route) -> None:
        # Every POST remains browser-intercepted; this test never invokes a provider write.
        refresh_calls.append((route.request.method, urlsplit(route.request.url).path))
        held_refreshes.append(route)

    page.route("**/v1/catalog/entities/*/refresh", hold_refresh)
    page.goto(f"{BASE}/?view=entities")
    host = page.locator(f'button[data-node-id="entity:{HOST_ENTITY_ID}"]')
    organizer = page.locator(f'button[data-node-id="entity:{ENTITY_ID}"]')
    host.click()
    inspector = page.get_by_role("complementary", name="Entity detail", exact=True)
    with page.expect_response(f"**/v1/catalog/entities/{HOST_ENTITY_ID}") as host_response:
        inspector.get_by_role("tab", name="Profile & sources", exact=True).click()
    host_payload = host_response.value.json()
    expect(inspector.get_by_text("Published host profile fixture.", exact=True)).to_be_visible()
    inspector.get_by_role("button", name="Refresh", exact=True).click()
    expect(inspector.get_by_role("button", name="Refreshing", exact=True)).to_be_disabled()
    assert len(held_refreshes) == 1

    organizer.click()
    expect(inspector.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    inspector.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(inspector.get_by_text("Published profile fixture.", exact=True)).to_be_visible()
    expect(inspector.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    refreshed_host = host_payload | {
        # If A's late result is treated as B's, it could trigger an unauthorized B auto-refresh.
        "refresh_due": True,
        "external_facts": [host_payload["external_facts"][0] | {"value": "Refreshed host profile fixture."}],
    }
    with page.expect_response(f"**/v1/catalog/entities/{HOST_ENTITY_ID}/refresh") as refresh_response:
        api.respond(held_refreshes[0], refreshed_host)
    refresh_response.value.finished()
    # Allow the resolved fetch and React effects to run before checking for a spurious B POST.
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(inspector.get_by_role("heading", name="Lakehouse Music", exact=True)).to_be_visible()
    expect(inspector.get_by_text("Published profile fixture.", exact=True)).to_be_visible()
    expect(inspector.get_by_text("Refreshed host profile fixture.", exact=True)).to_have_count(0)
    expect(inspector.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    assert refresh_calls == [("POST", f"/v1/catalog/entities/{HOST_ENTITY_ID}/refresh")]
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("scope", ["events", "map", "calendar", "overview", "entity", "topic"])
def test_event_titles_open_provider_without_changing_selection(release_page, width, scope):
    harness, _ = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    provider_url = "https://events.example.test/friday-jazz"
    page.context.route(provider_url, lambda route: route.fulfill(content_type="text/html", body="Provider event fixture"))

    def open_title(link):
        expect(link).to_have_attribute("href", provider_url)
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_have_attribute("rel", "noopener noreferrer")
        before = page.url
        with page.expect_popup() as opened:
            if width == 1440:
                link.press("Enter")
            else:
                link.click()
        popup = opened.value
        expect(popup).to_have_url(provider_url)
        popup.close()
        assert page.url == before

    query = {"events": "view=events", "map": "view=map", "calendar": "view=calendar",
             "overview": "view=entities", "entity": f"view=entities&entity={ENTITY_ID}",
             "topic": "view=entities&topic=jazz"}[scope]
    page.goto(f"{BASE}/?{query}&when=custom&start=2030-06-01&end=2030-06-30")
    if scope == "events":
        card = page.locator(".event-list .event-card").first
        open_title(card.get_by_role("link", name="Friday Night Jazz", exact=True))
        expect(card.get_by_role("button", name="Show details for Friday Night Jazz", exact=True)).to_have_attribute("aria-expanded", "false")
        # The non-link summary and the explicit disclosure control still expand/collapse once.
        card.locator(".event-card__date").click()
        expect(card.get_by_role("region")).to_be_visible()
        card.get_by_role("button", name="Hide details for Friday Night Jazz", exact=True).click()
        card.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    elif scope == "map":
        focus = page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True)
        open_title(page.locator(".map-preview-rail").get_by_role("link", name="Friday Night Jazz", exact=True))
        expect(focus).to_have_attribute("aria-pressed", "false")
        focus.click()
        expect(focus).to_have_attribute("aria-pressed", "true")
        card = page.locator(".map-selection .event-card")
    elif scope == "calendar":
        page.get_by_role("gridcell", name=re.compile(r"June 14.*1 event")).click()
        card = page.locator(".calendar-agenda .event-card")
        card.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    else:
        page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
        card = page.get_by_role("complementary", name="Event detail", exact=True).locator(".event-card")
    expect(card.get_by_role("region")).to_be_visible()
    expect(card.get_by_role("link", name=re.compile(r"^(Sign up|Join waitlist|View details|View event)$"))).to_have_count(0)
    open_title(card.get_by_role("link", name="Friday Night Jazz", exact=True))
    expect(card.get_by_role("region")).to_be_visible()


@pytest.mark.parametrize("scope", ["events", "map", "entity"])
@pytest.mark.parametrize("event_url", [None, "javascript:alert(1)"])
def test_event_titles_without_safe_provider_urls_remain_text(release_page, scope, event_url):
    harness, api = release_page
    api.event_url = event_url
    page = harness.page
    query = {"events": "view=events", "map": "view=map", "entity": f"view=entities&entity={ENTITY_ID}"}[scope]
    page.goto(f"{BASE}/?{query}")
    if scope == "map":
        expect(page.locator(".map-preview__copy strong")).to_have_text("Friday Night Jazz")
        expect(page.locator(".map-preview__copy strong a")).to_have_count(0)
        page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True).click()
        card = page.locator(".map-selection .event-card")
    elif scope == "entity":
        page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
        card = page.get_by_role("complementary", name="Event detail", exact=True).locator(".event-card")
    else:
        card = page.locator(".event-list .event-card").first
    expect(card.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(card.get_by_role("link", name="Friday Night Jazz", exact=True)).to_have_count(0)
    expect(page.locator('a[href^="javascript:"]')).to_have_count(0)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("scope", ["overview", "entity", "topic"])
def test_graph_event_details_match_map_without_prefetch(release_page, tmp_path, width, scope):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{BASE}/?view=map&when=custom&start=2030-06-01&end=2030-06-30")
    page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True).click()
    map_card = page.locator(".map-selection .event-card")
    expect(map_card.get_by_role("region")).to_be_visible()
    shared_parts = [".event-card__eyebrow", ".event-card__title", ".event-card__date",
                    ".event-card__meta", ".event-card__decision-strip", ".event-card__facts",
                    ".event-card__description"]
    expected = {part: map_card.locator(part).inner_text() for part in shared_parts}
    expected_links = map_card.get_by_role("link").evaluate_all("links => links.map(a => a.href)")
    graph_query = {"overview": "view=entities", "entity": f"view=entities&entity={ENTITY_ID}",
                   "topic": "view=entities&topic=jazz"}[scope]
    # Leave Map through its navigation so its effect cleans up outstanding tiles.
    page.get_by_role("button", name="Entities", exact=True).click()
    expect(page.get_by_role("application")).to_be_visible()
    if scope != "overview":
        page.goto(f"{BASE}/?{graph_query}")
    expect(page.get_by_role("application")).to_be_visible()
    expect(page.get_by_role("button", name="Text", exact=True)).to_have_count(0)
    node_id = f"event:{catalog_event()['canonical_event_id']}"
    node = page.locator(f'button[data-node-id="{node_id}"]')
    expect(node).to_be_visible()
    assert not any(path.startswith("/v1/catalog/events/") for _, path in api.calls)
    node.focus()
    expect(node).to_be_focused()
    node.press("Enter")
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    card = inspector.locator(".event-card")
    expect(card.get_by_role("region")).to_be_visible()
    assert {part: card.locator(part).inner_text() for part in shared_parts} == expected
    assert card.get_by_role("link").evaluate_all("links => links.map(a => a.href)") == expected_links
    expect(card.get_by_role("button", name="Hide details for Friday Night Jazz", exact=True)).to_have_count(0)
    # The complete host facts remain even when this bounded graph drew only the organizer.
    expect(card.get_by_text("Alex Example", exact=True)).to_be_visible()
    if scope != "topic":
        card.get_by_text("Explore connections (1)", exact=True).click()
        expect(card.get_by_role("button", name=re.compile("Lakehouse Music.*Organizer"))).to_be_visible()
        expect(card.get_by_text(re.compile("asserted by Fixture Jazz"))).to_be_visible()
        card.get_by_text("Explore connections (1)", exact=True).click()
    close_label = "Back to Lakehouse Music" if scope == "entity" else "Back to the graph"
    inspector.get_by_role("button", name=close_label, exact=True).click()
    node.click()
    expect(inspector.locator(".event-card")).to_be_visible()
    detail_path = f"/v1/catalog/events/{catalog_event()['canonical_event_id']}"
    assert api.calls.count(("GET", detail_path)) == (0 if scope == "topic" else 1)
    assert ("GET", f"/v1/catalog/entities/{ENTITY_ID}") not in api.calls
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    action = card.get_by_role("link", name="Friday Night Jazz", exact=True)
    action.scroll_into_view_if_needed()
    assert action.evaluate("""link => {
        const bounds = link.getBoundingClientRect();
        return document.elementFromPoint(bounds.x + bounds.width / 2,
            bounds.y + bounds.height / 2)?.closest('a') === link;
    }""")
    page.screenshot(path=str(tmp_path / f"graph-event-card-{scope}-{width}.png"), full_page=True)
    inspector.screenshot(path=str(tmp_path / f"graph-event-detail-{scope}-{width}.png"))


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("scope", ["overview", "entity", "topic"])
@pytest.mark.parametrize("role,name,target_id", [
    ("host", "Alex Example", HOST_ENTITY_ID),
    ("organizer", "Lakehouse Music", ENTITY_ID),
])
def test_graph_event_entities_resolve_the_selected_assertion(release_page, width, scope, role, name, target_id):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    source_id = ENTITY_ID if role == "host" else HOST_ENTITY_ID
    query = {"overview": "view=entities", "entity": f"view=entities&entity={source_id}",
             "topic": "view=entities&topic=jazz"}[scope]
    page.goto(f"{BASE}/?{query}")
    event_id = catalog_event()["canonical_event_id"]
    page.locator(f'button[data-node-id="event:{event_id}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    inspector.get_by_role("button", name=f"Explore {role} {name}", exact=True).click()
    expect(page.get_by_role("heading", name=name, exact=True).first).to_be_visible()
    assert api.entity_resolutions == [{"canonical_event_id": [event_id], "role": [role], "name": [name]}]
    assert parse_qs(urlsplit(page.url).query)["entity"] == [target_id]
    assert ("GET", f"/v1/catalog/entities/{target_id}/graph") in api.calls
    expect(page.get_by_role("application")).to_be_visible()
    expect(page.get_by_role("button", name="Text", exact=True)).to_have_count(0)
    expect(inspector).to_have_count(0)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("scope", ["overview", "entity", "topic"])
def test_graph_event_topic_opens_topic_graph(release_page, width, scope):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    query = {"overview": "view=entities", "entity": f"view=entities&entity={ENTITY_ID}",
             "topic": "view=entities&topic=jazz"}[scope]
    page.goto(f"{BASE}/?{query}")
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    inspector.get_by_role("button", name="Add jazz topic filter", exact=True).click()
    expect(page.get_by_role("heading", name="Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("application")).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["view"] == ["entities"] and params["topic"] == ["jazz"]
    assert "entity" not in params
    assert not api.entity_resolutions
    expect(page.get_by_role("button", name="Text", exact=True)).to_have_count(0)


@pytest.mark.parametrize("scope", ["overview", "entity", "topic"])
@pytest.mark.parametrize("status", [404, 503])
def test_graph_entity_resolution_failure_is_visible_and_retryable(release_page, scope, status):
    harness, api = release_page
    harness.allowed_console_error_fragments.append(f"status of {status}")
    api.resolution_status = status
    page = harness.page
    query = {"overview": "view=entities", "entity": f"view=entities&entity={ENTITY_ID}",
             "topic": "view=entities&topic=jazz"}[scope]
    page.goto(f"{BASE}/?{query}")
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    chip = inspector.get_by_role("button", name="Explore host Alex Example", exact=True)
    chip.click()
    message = "Alex Example is not indexed as an entity yet." if status == 404 else "Entity resolution unavailable"
    expect(page.get_by_role("main").get_by_role("alert")).to_contain_text(message)
    expect(inspector.locator(".event-card__title")).to_have_text("Friday Night Jazz")
    assert parse_qs(urlsplit(page.url).query).get("entity") != [HOST_ENTITY_ID]
    api.resolution_status = 200
    chip.click()
    expect(page.get_by_role("heading", name="Alex Example", exact=True).first).to_be_visible()
    expect(page.get_by_role("main").get_by_role("alert")).to_have_count(0)


@pytest.mark.parametrize("status", [404, 503])
def test_graph_event_details_retry_without_inventing_facts(release_page, status):
    harness, api = release_page
    harness.allowed_console_error_fragments.append(f"status of {status}")
    api.event_status = status
    page = harness.page
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    page.locator(f'button[data-node-id="event:{catalog_event()["canonical_event_id"]}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    expect(inspector.get_by_role("alert")).to_be_visible()
    expect(inspector.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(inspector.locator(".event-card__decision-strip")).to_have_count(0)
    expect(inspector.get_by_role("link", name="Friday Night Jazz", exact=True)).to_have_attribute(
        "href", "https://events.example.test/friday-jazz")
    api.event_status = 200
    inspector.get_by_role("button", name="Retry event details", exact=True).click()
    expect(inspector.locator(".event-card__description")).to_contain_text(catalog_event()["description"])
    expect(inspector.get_by_role("alert")).to_have_count(0)


def test_graph_event_selection_discards_late_details(release_page):
    harness, api = release_page
    api.second_event = True
    api.hold_events = True
    page = harness.page
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    first_id = catalog_event()["canonical_event_id"]
    with page.expect_request(f"**/v1/catalog/events/{first_id}"):
        page.locator(f'button[data-node-id="event:{first_id}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    expect(inspector.get_by_role("status")).to_contain_text("Loading event details")
    with page.expect_request(f"**/v1/catalog/events/{SECOND_EVENT_ID}"):
        page.locator(f'button[data-node-id="event:{SECOND_EVENT_ID}"]').click()
    expect(inspector.get_by_role("heading", name="Saturday Night Jazz", exact=True)).to_be_visible()
    assert len(api.held_events) == 2
    second = api.held_events[1]
    api.respond(second, second_catalog_event())
    expect(inspector.locator(".event-card__title")).to_have_text("Saturday Night Jazz")
    try:
        api.respond(api.held_events[0], catalog_event())
    except Exception as error:
        assert "Target closed" in str(error) or "Invalid InterceptionId" in str(error)
    expect(inspector.locator(".event-card__title")).to_have_text("Saturday Night Jazz")
    expect(inspector.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_have_count(0)


def test_graph_recurring_dates_load_their_own_event_facts(release_page):
    harness, api = release_page
    api.second_event = True
    api.recurring = True
    page = harness.page
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    first_id = catalog_event()["canonical_event_id"]
    page.locator(f'button[data-node-id="event:{first_id}"]').click()
    inspector = page.get_by_role("complementary", name="Event detail", exact=True)
    expect(inspector.locator(".event-card__description")).to_have_text(catalog_event()["description"])
    dates = inspector.get_by_role("region", name="Event dates", exact=True)
    dates.get_by_role("button", name=re.compile("Jun 15")).click()
    expect(inspector.locator(".event-card__date strong")).to_have_text("15")
    expect(inspector.locator(".event-card__description")).to_have_text(second_catalog_event()["description"])
    assert api.calls.count(("GET", f"/v1/catalog/events/{SECOND_EVENT_ID}")) == 1
    dates.get_by_role("button", name=re.compile("Jun 14")).click()
    expect(inspector.locator(".event-card__date strong")).to_have_text("14")
    expect(inspector.locator(".event-card__description")).to_have_text(catalog_event()["description"])
    assert api.calls.count(("GET", f"/v1/catalog/events/{first_id}")) == 1
    dates.get_by_role("button", name=re.compile("Jun 15")).click()
    inspector.get_by_role("button", name="Explore host Alex Example", exact=True).click()
    expect(page.get_by_role("heading", name="Alex Example", exact=True).first).to_be_visible()
    assert api.entity_resolutions == [{"canonical_event_id": [SECOND_EVENT_ID],
                                       "role": ["host"], "name": ["Alex Example"]}]


def test_discovery_entities_empty_state(release_page):
    harness, api = release_page
    api.empty_graph = True
    harness.page.goto(f"{BASE}/?view=entities")
    expect(harness.page.get_by_role("heading", name="No entities match", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_imported_profile_links_are_compact_and_read_only(release_page, tmp_path, width):
    harness, api = release_page
    api.social_links_only = True
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    expect(page.get_by_role("heading", name="Lakehouse Music", exact=True).first).to_be_visible()
    assert ("GET", f"/v1/catalog/entities/{ENTITY_ID}") not in api.calls
    expect(page.get_by_role("tab", name="Same name", exact=True)).to_have_count(0)
    page.get_by_role("tab", name="Profile & sources", exact=True).click()
    expect(page.get_by_role("link", name="X @social_builder", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Instagram @social_builder", exact=True)).to_be_visible()
    for url in ("https://x.com/social_builder", "https://www.instagram.com/social_builder",
                "https://events.example.test/lakehouse"):
        expect(page.locator(f'.entity-graph-inspector a[href="{url}"]')).to_have_count(1)
        expect(page.get_by_text(url, exact=True)).to_have_count(0)
    expect(page.get_by_text("Holding it is not a verification", exact=False)).to_have_count(0)
    page.get_by_role("tab", name="Profile & sources", exact=True).focus()
    page.keyboard.press("Tab")
    expect(page.locator(".entity-graph-inspector a").first).to_be_focused()
    assert api.calls.count(("GET", f"/v1/catalog/entities/{ENTITY_ID}")) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"linked-profiles-{width}.png"))


@pytest.mark.parametrize("avatar_status", [200, 404])
def test_social_profile_snapshots_are_separate_read_only_and_responsive(
    release_page, tmp_path, avatar_status
):
    harness, api = release_page
    api.social_profiles = True
    page = harness.page
    if avatar_status == 404:
        harness.allowed_console_error_fragments.append("404")
    page.route("https://pbs.twimg.com/**", lambda route: route.fulfill(
        status=avatar_status, content_type="image/png",
        body=_TRANSPARENT_MAP_TILE if avatar_status == 200 else b""
    ))
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    page.get_by_role("tab",name="Profile & sources",exact=True).click()
    x = page.get_by_role("region", name="X API", exact=True)
    expect(x.get_by_text("Public AI community",exact=True)).to_be_visible()
    expect(x.get_by_text("6,412 followers",exact=True)).to_be_visible()
    expect(x.get_by_text("Refresh unavailable; saved facts are shown.",exact=True)).to_be_visible()
    expect(page.get_by_role("region",name="Instagram API").get_by_text("4,300 followers",exact=True)).to_be_visible()
    expect(page.locator('.entity-graph-inspector a[href="https://x.com/social_builder"]')).to_have_count(1)
    expect(page.locator('.entity-graph-inspector a[href="https://www.instagram.com/social_builder"]')).to_have_count(1)
    if avatar_status == 200:
        expect(x.locator("img")).to_have_attribute("referrerpolicy", "no-referrer")
    else:
        expect(x.locator(".entity-social-profile__avatar svg")).to_be_visible()
        expect(x.locator("img")).to_have_count(0)
    expect(page.locator('a[href="https://pbs.twimg.com/avatar.png"]')).to_have_count(0)
    expect(page.get_by_role("button",name="Refresh",exact=True)).to_have_count(0)
    assert api.calls.count(("GET",f"/v1/catalog/entities/{ENTITY_ID}"))==1
    page.screenshot(path=str(tmp_path / "social-profile-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    x.scroll_into_view_if_needed()
    expect(x.get_by_text("6,412 followers",exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "social-profile-mobile.png"))


def test_discovery_entity_graph_retries_a_failed_read(release_page):
    harness, api = release_page
    api.graph_status = 503
    harness.allowed_console_error_fragments.append("503")
    page = harness.page
    page.goto(f"{BASE}/?view=entities&entity={ENTITY_ID}")
    expect(page.locator(".entity-graph-failure[role=alert]")).to_be_visible()
    api.graph_status = 200
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("heading", name="Lakehouse Music", exact=True).first).to_be_visible()
    assert api.calls.count(("GET", f"/v1/catalog/entities/{ENTITY_ID}/graph")) == 2


@pytest.mark.parametrize("profile", ["full", None])
def test_full_and_legacy_runtime_configs_preserve_chat_and_entity_navigation(release_page, profile):
    harness, api = release_page
    api.profile = profile
    page = harness.page
    page.goto(f"{BASE}/")
    expect(page.locator(".chat-view")).to_be_visible()
    expect(page.locator(".site-nav").get_by_role("button", name="Chat", exact=True)).to_be_visible()
    expect(page.locator(".site-nav").get_by_role("button", name="Entities", exact=True)).to_be_visible()
    page.locator(".site-nav").get_by_role("button", name="Events", exact=True).click()
    page.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    expect(page.get_by_role("button", name="Explore organizer Lakehouse Music", exact=True)).to_be_visible()
    page.get_by_role("button", name="Events Concierge", exact=True).click()
    expect(page.locator(".chat-view")).to_be_visible()


def test_unknown_runtime_profile_fails_closed(release_page):
    harness, api = release_page
    api.profile = "future-profile"
    harness.page.goto(f"{BASE}/?view=chat")
    expect(harness.page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(harness.page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)


def test_unresolved_config_never_renders_deferred_navigation(release_page):
    harness, api = release_page
    api.hold_config = True
    page = harness.page
    page.goto(f"{BASE}/?view=chat")
    expect(page.locator(".app-loading")).to_be_visible()
    expect(page.get_by_role("navigation")).to_have_count(0)
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    assert api.calls == [("GET", "/v1/ui-config")]
    assert api.held
    api.config(api.held.pop())
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()


def test_config_failure_can_retry_without_exposing_onboarding_or_chat(release_page):
    harness, api = release_page
    api.config_status = 503
    harness.allowed_console_error_fragments.append("503")
    page = harness.page
    page.goto(f"{BASE}/?view=chat")
    expect(page.get_by_role("main").get_by_role("alert")).to_contain_text("Sign-in is temporarily unavailable. Please try again.")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    expect(page.get_by_role("navigation")).to_have_count(0)
    assert api.calls == [("GET", "/v1/ui-config")]
    api.config_status = 200
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()


def test_discovery_settings_suppress_automated_registration_and_calendar_claims(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/settings/account")
    expect(page.get_by_role("heading", name="Event discovery", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Activity", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Security", exact=True)).to_have_count(0)
    expect(page.get_by_text("Find and handle it.", exact=False)).to_have_count(0)
    expect(page.get_by_text("Your calendar stays true.", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/activity")
    expect(page.get_by_role("heading", name="Event discovery", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Withdraw", exact=True)).to_have_count(0)
    assert not any(path in {"/v1/me/requests", "/v1/me/registrations", "/v1/me/tasks"} for _, path in api.calls)
    page.goto(f"{BASE}/settings")
    expect(page.get_by_role("heading", name="Profile", exact=True)).to_be_visible()
    expect(page.get_by_text("Relay inbox", exact=True)).to_have_count(0)
    expect(page.get_by_text("sends confirmations", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/taste")
    expect(page.get_by_text("Use topic filters", exact=False)).to_be_visible()
    expect(page.get_by_text("keeps refining the order", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/security")
    expect(page.get_by_role("heading", name="Account security", exact=True)).to_be_visible()
    expect(page.locator(".preview-banner")).to_have_count(0)
    expect(page.get_by_role("button")).to_have_count(0)


def test_full_profile_retains_explicitly_inert_security_preview(release_page):
    harness, api = release_page
    api.profile = "full"
    page = harness.page
    page.goto(f"{BASE}/settings/security")
    expect(page.get_by_role("heading", name="Security", exact=True)).to_be_visible()
    expect(page.get_by_role("note")).to_contain_text("Design preview")
    expect(page.get_by_role("link", name="Security", exact=True)).to_be_visible()
    buttons = page.get_by_role("button")
    assert buttons.count() > 0
    for button in buttons.all():
        expect(button).to_be_disabled()


@pytest.mark.parametrize("profile", ["discovery", "full"])
@pytest.mark.parametrize("width", [1440, 390])
def test_profiles_keep_map_and_calendar_browsing(release_page, tmp_path, profile, width):
    harness, api = release_page
    api.profile = profile
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{BASE}/?view=map&when=custom&start=2030-06-01&end=2030-06-30")
    expect(page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True)).to_be_visible()
    graph = page.get_by_role("button", name="View Friday Night Jazz in Lakehouse Music's graph", exact=True)
    expect(graph).to_be_visible()
    expect(page.locator(".map-preview-rail").get_by_role("link", name="Friday Night Jazz", exact=True)).to_be_visible()
    page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True).click()
    map_card = page.locator(".map-selection .event-card")
    expect(map_card.get_by_role("region")).to_be_visible()
    map_details = map_card.locator(".event-card__expansion-inner").inner_text()
    expect(map_card.get_by_role("button", name="Explore host Alex Example", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / f"map-card-{width}.png"), full_page=True)
    nav = ".site-nav" if width > 700 else ".mobile-nav"
    page.locator(nav).get_by_role("button", name="Calendar", exact=True).click()
    expect(page.get_by_role("grid", name="June 2030", exact=True)).to_be_visible()
    day = page.get_by_role("gridcell", name=re.compile(r"June 14.*1 event"))
    expect(day).to_be_visible()
    day.click()
    calendar_card = page.locator(".calendar-agenda .event-card")
    calendar_card.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    expect(calendar_card.get_by_role("region")).to_be_visible()
    assert calendar_card.locator(".event-card__expansion-inner").inner_text() == map_details
    expect(calendar_card.get_by_role("button", name="Explore host Alex Example", exact=True)).to_be_visible()
    expect(calendar_card).to_have_class(re.compile(r"is-compact"))
    assert calendar_card.evaluate("""card => {
        const bounds = card.getBoundingClientRect();
        const details = card.querySelector('.event-card__expansion-inner').getBoundingClientRect();
        return bounds.width > 520 || details.left - bounds.left < 24;
    }""")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"calendar-card-{width}.png"), full_page=True)
    assert ("GET", "/v1/catalog/events/summary") in api.calls


def _install_map_catalog(page: Page, api: ReleaseApi, pages: list[list[dict]]) -> list[dict[str, list[str]]]:
    """Serve paged catalog fixtures without a database, geocoder or provider request."""
    queries: list[dict[str, list[str]]] = []

    def catalog(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/events"))
        cursor = query.get("cursor", [None])[0]
        index = int(cursor.removeprefix("fixture-page-")) if cursor else 0
        assert index < len(pages), f"Unexpected catalog cursor: {cursor}"
        api.respond(route, {
            "items": pages[index],
            "next_cursor": f"fixture-page-{index + 1}" if index + 1 < len(pages) else None,
            "providers": [{"source_key": "fixture-techweek", "label": "SF Tech Week 2030",
                           "display_name": "SF Tech Week 2030", "publisher": "Tech Week",
                           "provider": "tech_week_mcp", "seed_url": "https://events.example.test",
                           "event_count": sum(map(len, pages))}],
            "topic_facets": [], "city_facets": [{"city": "sanfrancisco", "event_count": sum(map(len, pages))}],
        })

    page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    return queries


def _map_catalog_event(event_id: str, title: str, *, mapped: bool = False) -> dict:
    return catalog_event() | {
        "canonical_event_id": event_id, "title": title, "city": "sanfrancisco",
        "venue_name": "SOMA", "latitude": 37.7749 if mapped else None,
        "longitude": -122.4194 if mapped else None,
        "description": f"Published details for {title}.",
        "source_keys": ["fixture-techweek"], "providers": ["Tech Week"],
        "sources": [{"source_key": "fixture-techweek", "source": "tech_week_mcp",
                     "label": "SF Tech Week 2030", "registration_url": "https://events.example.test/friday-jazz"}],
    }


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("mapped_second", [False, True])
def test_map_retains_unlocated_events_filters_details_and_pagination(release_page, width, mapped_second):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    first = _map_catalog_event(catalog_event()["canonical_event_id"], "Venue Pending Mixer")
    second = _map_catalog_event(SECOND_EVENT_ID, "Another Venue Pending Mixer", mapped=mapped_second)
    queries = _install_map_catalog(page, api, [[first], [second]])
    page.goto(f"{BASE}/?view=events&source=fixture-techweek&city=sanfrancisco&when=custom&start=2030-06-01&end=2030-06-30&price=any")
    expect(page.get_by_role("heading", name=first["title"], exact=True)).to_be_visible()
    filters = {key: value for key, value in parse_qs(urlsplit(page.url).query).items() if key != "view"}
    nav = page.locator(".site-nav" if width > 700 else ".mobile-nav")
    nav.get_by_role("button", name="Map", exact=True).click()

    rail = page.locator(".map-preview-rail")
    missing_list = rail.get_by_role("button", name=re.compile(r"^Without map locations"))
    expect(missing_list).to_have_attribute("aria-pressed", "true")
    expect(rail.get_by_role("button", name=f"Show details for {first['title']}", exact=True)).to_be_visible()
    expect(page.locator(".map-marker")).to_have_count(0)
    assert {key: value for key, value in parse_qs(urlsplit(page.url).query).items() if key != "view"} == filters
    assert parse_qs(urlsplit(page.url).query)["view"] == ["map"]

    rail.get_by_role("button", name=f"Show details for {first['title']}", exact=True).click()
    selection = page.locator(".map-selection .event-card")
    expect(selection.get_by_role("region")).to_be_visible()
    expect(selection).to_contain_text(first["description"])
    expect(selection.locator('a[href="https://events.example.test/friday-jazz"]')).to_be_visible()

    page.get_by_role("button", name="Load more", exact=True).click()
    expect(page.get_by_role("button", name="Load more", exact=True)).to_have_count(0)
    expect(rail.get_by_role("button", name=f"Show details for {first['title']}", exact=True)).to_be_visible()
    expect(missing_list).to_have_attribute("aria-pressed", "true")
    expect(selection).to_contain_text(first["description"])
    expect(page.locator(".map-marker")).to_have_count(int(mapped_second))
    if mapped_second:
        rail.get_by_role("button", name=re.compile(r"^In this area")).click()
        expect(rail.get_by_role("button", name=f"Focus {second['title']} on map", exact=True)).to_be_visible()
        missing_list.click()
    else:
        expect(rail.get_by_role("button", name=f"Show details for {second['title']}", exact=True)).to_be_visible()
    paged_query = next(query for query in queries if query.get("cursor") == ["fixture-page-1"])
    assert {key: value for key, value in paged_query.items() if key != "cursor"} == queries[0]
    assert paged_query["source_key"] == ["fixture-techweek"]
    assert paged_query["city"] == ["sanfrancisco"]
    assert {"starts_after", "starts_before"}.issubset(paged_query)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    nav.get_by_role("button", name="Events", exact=True).click()
    expect(page.get_by_role("heading", name=first["title"], exact=True)).to_be_visible()
    assert {key: value for key, value in parse_qs(urlsplit(page.url).query).items() if key != "view"} == filters


@pytest.mark.parametrize("width", [1440, 390])
def test_map_keeps_mapped_and_unlocated_events_in_separate_accessible_lists(release_page, width):
    harness, api = release_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    mapped = _map_catalog_event(catalog_event()["canonical_event_id"], "Mapped Builders Meetup", mapped=True)
    unlocated = _map_catalog_event(SECOND_EVENT_ID, "Venue Pending Mixer")
    _install_map_catalog(page, api, [[mapped, unlocated]])
    page.goto(f"{BASE}/?view=map&source=fixture-techweek&city=sanfrancisco&when=custom&start=2030-06-01&end=2030-06-30&price=any")

    rail = page.locator(".map-preview-rail")
    area_list = rail.get_by_role("button", name=re.compile(r"^In this area"))
    missing_list = rail.get_by_role("button", name=re.compile(r"^Without map locations"))
    expect(area_list).to_have_attribute("aria-pressed", "true")
    expect(rail.get_by_role("button", name=f"Focus {mapped['title']} on map", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name=f"Show details for {unlocated['title']}", exact=True)).to_have_count(0)
    expect(page.locator(".map-marker")).to_have_count(1)

    missing_list.click()
    expect(missing_list).to_have_attribute("aria-pressed", "true")
    expect(rail.get_by_role("button", name=f"Focus {mapped['title']} on map", exact=True)).to_have_count(0)
    rail.get_by_role("button", name=f"Show details for {unlocated['title']}", exact=True).click()
    expect(page.locator(".map-selection .event-card")).to_contain_text(unlocated["description"])
    expect(page.locator(".map-marker")).to_have_count(1)

    area_list.click()
    expect(area_list).to_have_attribute("aria-pressed", "true")
    rail.get_by_role("button", name=f"Focus {mapped['title']} on map", exact=True).click()
    expect(page.locator(".map-selection .event-card")).to_contain_text(mapped["description"])
    expect(page.locator(".map-marker")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("local_demo", [True, False])
def test_profile_avatar_reads_use_the_current_auth_contract(release_page, local_demo):
    harness, api = release_page
    api.local_demo = local_demo
    page = harness.page
    avatar_reads = []

    def identity(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "notify_email": "browser@example.test", "interests": [],
            "preference_revision": 1, "local_demo": local_demo, "is_admin": False,
            "profile": {"display_name": "Avatar reader", "time_zone": None, "revision": 1,
                        "avatar_url": "/v1/me/avatar?v=fixture"},
        }))

    def avatar(route):
        tenant = route.request.headers.get("x-ec-tenant-id")
        avatar_reads.append(tenant)
        authorized = bool(tenant) if local_demo else tenant is None
        route.fulfill(status=200 if authorized else 401, content_type="image/png", body=_TRANSPARENT_MAP_TILE)

    page.route("**/v1/me", identity)
    page.route("**/v1/me/avatar?*", avatar)
    page.goto(f"{BASE}/settings")
    expect(page.get_by_role("img", name="Your profile photo", exact=True)).to_be_visible()
    page.wait_for_function("document.querySelector('.profile-photo__image')?.naturalWidth > 0", timeout=5000)
    page.goto(f"{BASE}/?view=events")
    expect(page.locator(".account-menu__avatar")).to_be_visible()
    page.wait_for_function("document.querySelector('.account-menu__avatar')?.naturalWidth > 0", timeout=5000)
    assert len(avatar_reads) == 2
    assert all(avatar_reads) if local_demo else not any(avatar_reads)
    assert len(set(avatar_reads)) == 1


def test_discovery_defers_api_keys_in_navigation_deep_links_and_history(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    page.get_by_role("button", name=re.compile(r"^Account menu")).click()
    expect(page.get_by_role("menuitem", name=re.compile(r"^API keys"))).to_have_count(0)
    page.goto(f"{BASE}/settings/api-keys")
    expect(page.get_by_role("heading", name="Account access", exact=True)).to_be_visible()
    expect(page.get_by_text("API keys are not available in this release.", exact=False)).to_be_visible()
    expect(page.get_by_role("navigation").get_by_role("link", name="API keys", exact=True)).to_have_count(0)
    expect(page.get_by_role("textbox", name="Key name", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Create key", exact=True)).to_have_count(0)
    page.get_by_role("main").get_by_role("link", name="Account and data", exact=True).click()
    expect(page.get_by_role("heading", name="Account and data", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Account access", exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role("heading", name="Account and data", exact=True)).to_be_visible()
    assert not any(path.startswith("/v1/me/api-keys") for _, path in api.calls)


def test_full_profile_retains_api_key_records_with_accurate_capability_text(release_page):
    harness, api = release_page
    api.profile = "full"
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    page.get_by_role("button", name=re.compile(r"^Account menu")).click()
    page.get_by_role("menuitem", name="API keys Development key records", exact=True).click()
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_visible()
    expect(page.get_by_text("Authentication with these keys is not available yet.", exact=False)).to_be_visible()
    expect(page.get_by_role("textbox", name="Key name", exact=True)).to_be_visible()
    expect(page.get_by_text("No keys yet.", exact=True)).to_be_visible()
    assert ("GET", "/v1/me/api-keys") in api.calls
