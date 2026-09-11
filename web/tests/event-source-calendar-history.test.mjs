import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const conciergeApp = readFileSync(
  new URL("../components/concierge-app.tsx", import.meta.url),
  "utf8",
);
const calendarView = readFileSync(
  new URL("../components/calendar-view.tsx", import.meta.url),
  "utf8",
);
const filterBar = readFileSync(
  new URL("../components/filter-bar.tsx", import.meta.url),
  "utf8",
);
const eventsView = readFileSync(
  new URL("../components/events-view.tsx", import.meta.url),
  "utf8",
);
const mapView = readFileSync(
  new URL("../components/map-view.tsx", import.meta.url),
  "utf8",
);

test("selecting a source opens retained source history and clears conflicting places", () => {
  assert.match(conciergeApp, /const handleSourceSelect = useCallback/);
  assert.match(conciergeApp, /datePreset: "source"/);
  assert.match(conciergeApp, /customStart: ""/);
  assert.match(conciergeApp, /customEnd: ""/);
  assert.match(conciergeApp, /city: ""/);
  assert.match(conciergeApp, /cities: \[\]/);
  assert.match(conciergeApp, /locationScopes: \[\]/);
  assert.match(conciergeApp, /filters\.datePreset === "source" && events\[0\]/);
  assert.match(conciergeApp, /filters\.datePreset === "source"/);
});

test("calendar navigation requests the selected range from the server", () => {
  assert.match(calendarView, /onRangeChange: \(anchor: Date, mode: CalendarMode\) => void/);
  assert.match(calendarView, /const shiftRange = [\s\S]*?onRangeChange\(next, mode\)/);
  assert.match(calendarView, /const goToToday = [\s\S]*?onRangeChange\(today, mode\)/);
  assert.match(conciergeApp, /onRangeChange=\{handleCalendarRangeChange\}/);
});

test("source inventory counts identify their bounded date-range scope", () => {
  // The suggestion rows and the chip editor's option rows count the same
  // window, so they read the scope off one shared suffix.
  assert.match(
    filterBar,
    /const rangeDetailSuffix = filters\.datePreset === "source"\s+\? "retained"\s+: filters\.datePreset === "all" \? "upcoming" : "in range";/,
  );
  assert.equal(filterBar.match(/event_count\} \$\{rangeDetailSuffix\}/g)?.length, 4);
  assert.doesNotMatch(filterBar, /provider\.event_count\} events/);
});

test("historical and source windows are not presented as future-only results", () => {
  assert.match(eventsView, /sourceName\s*\? `\$\{sourceName\} events`/);
  assert.match(eventsView, /historicalWindow\s*\? "Events"/);
  assert.match(eventsView, /aria-label="Sort events"/);
  assert.match(eventsView, /<option value="soonest">Soonest first<\/option>/);
  assert.match(eventsView, /<option value="latest">Latest first<\/option>/);
  assert.match(conciergeApp, /sourceName=\{currentProvider\?\.display_name\}/);
  assert.match(conciergeApp, /historicalWindow=\{historicalWindow\}/);
});

test("the paged map view discloses and continues an incomplete result set", () => {
  assert.match(conciergeApp, /const generation = catalogGeneration\.current/);
  assert.match(conciergeApp, /if \(generation !== catalogGeneration\.current\) return/);
  assert.match(conciergeApp, /<MapView[\s\S]*?hasMore=\{Boolean\(nextCursor\)\}/);
  assert.match(mapView, /more available/);
  assert.match(mapView, /onClick=\{onLoadMore\}/);
});

test("the calendar continues only the selected day, never the whole range", () => {
  // The grid shows counts, so paging a range through the browser to derive them
  // read every event to display a number. Only the open day is continued.
  assert.match(conciergeApp, /<CalendarView[\s\S]*?dayHasMore=\{Boolean\(dayCursor\)\}/);
  assert.match(conciergeApp, /<CalendarView[\s\S]*?onDayLoadMore=\{handleDayLoadMore\}/);
  assert.doesNotMatch(conciergeApp.match(/<CalendarView[\s\S]*?\/>/)?.[0] ?? "", /onLoadMore=/);
  assert.match(calendarView, /onClick=\{onDayLoadMore\}/);
  assert.doesNotMatch(calendarView, /onLoadMore/);
  assert.match(
    conciergeApp,
    /if \(view === "calendar"\) return;\n\s*void loadCatalog\(filters\)/,
  );
});

test("filter reloads do not display results from the previous filter scope", () => {
  const loadStart = conciergeApp.match(
    /const loadCatalog = useCallback[\s\S]*?try \{/,
  )?.[0];
  assert.ok(loadStart);
  assert.match(loadStart, /setCatalogLoading\(true\)/);
  assert.match(loadStart, /setEvents\(\[\]\)/);
  assert.match(loadStart, /setNextCursor\(null\)/);
});

test("historical empty states describe retained evidence instead of claiming absence", () => {
  assert.match(calendarView, /No retained events/);
  assert.match(calendarView, /rolled off before archival capture/);
  assert.match(eventsView, /No retained events match/);
});
