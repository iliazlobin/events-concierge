import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import * as calendar from "../lib/calendar.ts";
import * as dates from "../lib/date.ts";
import * as discovery from "../lib/event-discovery.ts";
import * as facets from "../lib/event-facets.ts";
import * as links from "../lib/event-links.ts";
import * as prices from "../lib/event-price.ts";
import * as topics from "../lib/event-topics.ts";
import * as images from "../lib/event-image.ts";
import * as mapDays from "../lib/map-days.ts";
import * as mapLocations from "../lib/map-locations.ts";
import * as mapViewport from "../lib/map-viewport.ts";
import * as muse from "../lib/muse.ts";

const require = createRequire(import.meta.url);
/** Render the real TSX components without starting Next or installing a DOM emulator. */
function component(path, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(new URL(`../components/${path}`, import.meta.url), "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", code)(name => Object.hasOwn(dependencies, name) ? dependencies[name] : require(name), module, module.exports);
  return module.exports;
}
const context = component("event-actions-context.tsx");
const icon = component("muse-icon.tsx");
const actionDependencies = { "@/lib/muse": muse, "@/components/event-actions-context": context, "@/components/muse-icon": icon };
const action = component("event-muse-action.tsx", actionDependencies);
const card = component("event-card.tsx", {
  "@/lib/date": dates, "@/lib/event-discovery": discovery, "@/lib/event-facets": facets,
  "@/lib/event-links": links, "@/lib/event-price": prices, "@/lib/event-topics": topics,
  "@/components/event-actions-context": context, "@/components/event-muse-action": action,
});
const list = component("event-list.tsx", { "@/lib/event-discovery": discovery, "@/components/event-card": card });
const week = component("calendar-view.tsx", { "@/lib/calendar": calendar, "@/lib/date": dates, "@/lib/event-topics": topics,
  "@/components/event-list": list, "@/components/event-muse-action": action });
const preview = component("map-preview-rail.tsx", {
  "@/lib/date": dates, "@/lib/event-image": images, "@/lib/event-discovery": discovery,
  "@/lib/event-facets": facets, "@/lib/event-links": links, "@/lib/map-days": mapDays,
  "@/lib/map-viewport": mapViewport, "@/lib/map-locations": mapLocations,
  "@/components/event-actions-context": context, "@/components/event-muse-action": action,
});
const event = {
  canonical_event_id: "22222222-2222-4222-8222-222222222222", title: "Free design evening",
  start_at: "2030-06-14T19:30:00-07:00", end_at: null, price_status: "free",
  event_status: "scheduled", registration_status: "open", registerable: true,
  registration_urls: ["https://lu.ma/design-evening"], description: "An evening of design discussions.",
  city: "San Francisco", venue_name: "Studio", latitude: 37.7, longitude: -122.4,
  host_names: [], speaker_names: [], partner_names: [], organizer_name: null, attendance_count: null,
  price_min_cents: null, price_max_cents: null, price_currency: null, topics: [],
  calendar_labels: ["Design calendar"], source_keys: ["other-calendar", "design-calendar"],
  sources: [
    { label: "Other calendar", source_key: "other-calendar", registration_url: "https://lu.ma/design-evening" },
    { label: "Design calendar", source_key: "design-calendar", registration_url: "https://lu.ma/design-evening" },
  ],
};
const museActions = (changes = {}) => ({ statuses: new Map(), pendingIds: new Set(), onSignup: () => {}, ...changes });
function withActions(element, value) {
  return renderToStaticMarkup(createElement(context.EventActionsProvider, { value }, element));
}
function previewElement(value = event) {
  return createElement(preview.MapPreviewRail, {
    events: [value], unmappedEvents: [], mappedCount: 1, showUnmapped: false,
    onAreaClear: () => {}, onListChange: () => {}, selectedId: null, onSelect: () => {},
    groups: [], model: { cells: [], keys: [], maxTotal: 1, totalInView: 1, activeInView: 1, activeTotal: 1 },
    activeDay: null, daySectionRefs: { current: new Map() }, onDayActivate: () => {}, onDayFit: () => {},
  });
}

test("source actions resolve the displayed calendar, preserve fallback IDs, and skip blanks", () => {
  assert.deepEqual(context.eventSourceFilter(event), { label: "Design calendar", sourceKey: "design-calendar" });
  assert.deepEqual(context.eventSourceFilter({ ...event, calendar_labels: [" ", "Design calendar"] }), { label: "Design calendar", sourceKey: "design-calendar" });
  assert.deepEqual(context.eventSourceFilter({ ...event, calendar_labels: [], sources: [], source_keys: ["", "retained-source"], providers: ["Luma"] }), { label: "Luma", sourceKey: "retained-source" });
  assert.deepEqual(context.eventSourceFilter({ ...event, calendar_labels: [], sources: [], source_keys: [], providers: [] }), { label: "Event", sourceKey: null });
});

test("shared Muse controls stay absent without enabled account context", () => {
  assert.equal(renderToStaticMarkup(createElement(action.EventMuseAction, { event })), "");
  assert.equal(withActions(createElement(action.EventMuseAction, { event }), { onSourceSelect: () => {} }), "");
  assert.doesNotMatch(renderToStaticMarkup(createElement(card.EventCard, { event, expanded: false })), /Sign up with Muse/);
  assert.doesNotMatch(renderToStaticMarkup(previewElement()), /Sign up with Muse/);
});

test("Muse sits in event highlights across normal and compact cards", () => {
  for (const compact of [false, true]) {
    const html = withActions(createElement(card.EventCard, { event, expanded: false, compact }), { muse: museActions() });
    const meta = html.slice(html.indexOf('<div class="event-card__meta">'), html.indexOf('<ul class="event-card__summary-facts"'));
    assert.doesNotMatch(meta, /event-muse-action/);
    assert.match(html, /<ul class="event-card__summary-facts" aria-label="Event highlights">.*aria-label="Sign up with Muse for Free design evening"/);
    assert.match(html, /title="Sign up with Muse"/);
    assert.doesNotMatch(html, /Select for Muse|checkbox/);
  }
});

test("pending and existing tasks retain the same action without duplicate signup intent", () => {
  const existing = withActions(createElement(action.EventMuseAction, { event: { ...event, start_at: "2020-01-01T00:00:00Z" } }), {
    muse: museActions({ statuses: new Map([[event.canonical_event_id, "registered"]]) }),
  });
  assert.match(existing, /aria-label="View registration for Free design evening"/);
  assert.match(existing, /Registered · reported by Muse/);
  assert.doesNotMatch(existing, /disabled/);
  const pending = withActions(createElement(action.EventMuseAction, { event }), {
    muse: museActions({ pendingIds: new Set([event.canonical_event_id]) }),
  });
  assert.match(pending, /disabled="" aria-busy="true"/);
  assert.match(pending, /Queueing with Muse/);
  const paid = withActions(createElement(action.EventMuseAction, { event: { ...event, price_status: "paid" } }), { muse: museActions() });
  assert.match(paid, /disabled/);
  assert.match(paid, /Muse currently supports free events/);
});

test("the shared action delegates one account-owned callback and isolates the surrounding card", () => {
  const selected = [];
  const isolated = component("event-muse-action.tsx", { ...actionDependencies,
    "react": { ...require("react"), useRef: () => ({ current: null }), useEffect: () => {} },
    "@/components/event-actions-context": { useEventActions: () => ({ muse: museActions({ onSignup: value => selected.push(value) }) }) },
  });
  const control = isolated.EventMuseAction({ event });
  const calls = [];
  control.props.onClick({ preventDefault: () => calls.push("prevent"), stopPropagation: () => calls.push("stop") });
  assert.deepEqual(calls, ["prevent", "stop"]);
  assert.deepEqual(selected, [event]);
});

test("map previews expose independent calendar, source, focus, and Muse controls", () => {
  const html = withActions(previewElement(), { onSourceSelect: () => {}, muse: museActions() });
  assert.match(html, /class="map-preview__location-focus" type="button" aria-label="Focus Free design evening on map"/);
  assert.match(html, /class="map-preview__date-link" href="https:\/\/calendar.google.com\/calendar\/render\?/);
  assert.match(html, /class="map-preview__source-filter" aria-label="View source Design calendar"/);
  assert.match(html, /class="map-preview__calendar-link" href="https:\/\/calendar.google.com\/calendar\/render\?/);
  assert.match(html, /aria-label="Add Free design evening .* to Google Calendar"/);
  assert.match(html, /<ul class="map-preview__highlights" aria-label="Event highlights">.*Sign up with Muse/);
  const start = html.indexOf('<button class="map-preview__location-focus"');
  const focusButton = html.slice(start, html.indexOf("</button>", start) + 9);
  assert.doesNotMatch(focusButton, /<a |source-filter|event-muse-action/);
});


test("image previews preserve keyboard map focus while the visible time stays a calendar link", () => {
  const html = withActions(previewElement({ ...event, image_url: "https://images.example.test/event.jpg" }), { muse: museActions() });
  assert.match(html, /class="map-preview__select" type="button" aria-label="Focus Free design evening on map"/);
  assert.match(html, /class="map-preview__image"/);
  assert.match(html, /class="map-preview__calendar-link"/);
  assert.doesNotMatch(html, /map-preview__location-focus/);
});

test("week previews expose Muse without nesting a button in the selectable day", () => {
  const key = dates.localDateKey(event.start_at);
  const props = {
    summary: { days: [{ start_day: key, event_count: 1, topics: [] }], total_event_count: 1, time_zone: "UTC" },
    summaryCovered: true, dayEvents: [], dayPreviews: new Map([[key, [event]]]),
    onDaySelect: () => {}, focusDate: key, mode: "week", onRangeChange: () => {}, loading: false,
    dayLoading: false, error: null, dayHasMore: false, dayLoadingMore: false, onDayLoadMore: () => {},
    topics: [], activeTopics: [], onSourceSelect: () => {}, onFacetSelect: () => {}, onTopicSelect: () => {},
    onEventTopicSelect: () => {}, onTopicsClear: () => {},
  };
  const html = withActions(createElement(week.CalendarView, props), { muse: museActions() });
  assert.match(html, /role="gridcell" tabindex="0"/);
  assert.match(html, /class="calendar-week-event__meta".*Sign up with Muse for Free design evening/);
  assert.doesNotMatch(html, /<button[^>]*role="gridcell"/);
  assert.doesNotMatch(html, /class="calendar-week-day__events" aria-hidden="true"/);
});
