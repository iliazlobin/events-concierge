import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  calendarDayCategories,
  calendarLegendTopics,
  calendarRangeLabel,
  calendarWeekSpanLabel,
} from "../lib/calendar.ts";
import { eventTopicTone } from "../lib/event-topics.ts";

const calendarView = readFileSync(
  new URL("../components/calendar-view.tsx", import.meta.url),
  "utf8",
);
const conciergeApp = readFileSync(
  new URL("../components/concierge-app.tsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);
const apiClient = readFileSync(
  new URL("../lib/api.ts", import.meta.url),
  "utf8",
);

function event(id, topics = []) {
  return {
    canonical_event_id: id,
    topics,
  };
}

test("calendar category summaries count events once per topic and retain unclassified events", () => {
  const categories = calendarDayCategories([
    event("one", ["music", "community", "music"]),
    event("two", ["music"]),
    event("three", ["ai"]),
    event("four"),
  ], 4);

  assert.deepEqual(categories, [
    { topic: "music", label: "Music", eventCount: 2 },
    { topic: "ai", label: "AI", eventCount: 1 },
    { topic: "community", label: "Community", eventCount: 1 },
    { topic: "other", label: "Other", eventCount: 1 },
  ]);
});

test("calendar category summaries are complete unless a caller requests a limit", () => {
  const events = [
    event("one", ["music", "community", "ai", "arts", "technology"]),
  ];

  assert.equal(calendarDayCategories(events).length, 5);
  assert.equal(calendarDayCategories(events, 2).length, 2);
});

test("calendar legend counts stay synchronized with fully filtered visible events", () => {
  const topics = [
    { topic: "technology", label: "Technology", event_count: 11 },
    { topic: "board-games", label: "Board games", event_count: 1 },
    { topic: "ai", label: "AI", event_count: 17 },
    { topic: "community", label: "Community", event_count: 8 },
  ];

  assert.deepEqual(
    calendarLegendTopics(
      [event("mahjong", ["technology", "board-games", "ai"])],
      topics,
      ["technology", "board-games"],
    ),
    [
      { topic: "board-games", label: "Board games", event_count: 1 },
      { topic: "technology", label: "Technology", event_count: 1 },
      { topic: "ai", label: "AI", event_count: 1 },
    ],
  );
});

test("calendar legend retains active filters when their intersection is empty", () => {
  assert.deepEqual(
    calendarLegendTopics(
      [],
      [{ topic: "music", label: "Music", event_count: 31 }],
      ["music", "sports"],
    ),
    [
      { topic: "music", label: "Music", event_count: 0 },
      { topic: "sports", label: "Sports", event_count: 0 },
    ],
  );
});

test("calendar range headings stay natural across view modes and year boundaries", () => {
  assert.equal(
    calendarRangeLabel(new Date(2026, 7, 1), new Date(2026, 7, 31), "month", "en-US"),
    "August 2026",
  );
  assert.equal(
    calendarRangeLabel(new Date(2026, 7, 2), new Date(2026, 7, 8), "week", "en-US"),
    "Aug 2 – 8, 2026",
  );
  assert.equal(
    calendarRangeLabel(new Date(2026, 7, 1), new Date(2027, 0, 31), "six-months", "en-US"),
    "August 2026 – January 2027",
  );
});

test("the calendar reads its range as counts and only its open day as events", () => {
  // A month of this catalog is thousands of events, but the grid renders only
  // totals and topic chips. Paging the range to derive them cost one sequential
  // request per 72 events and started over on every reload.
  assert.match(calendarView, /summary: CatalogDaySummary \| null/);
  assert.match(calendarView, /calendarSummaryByDate\(summary\)/);
  assert.match(calendarView, /calendarSummaryDayCategories\(day\)/);
  assert.match(calendarView, /onDaySelect\(selectedKey\)/);
  assert.match(conciergeApp, /getCatalogSummary\(tenantId, nextFilters, timeZone\)/);
  assert.match(conciergeApp, /getCatalogDayPage\(\s*tenantId,\s*requestFilters,\s*dayKey,/);
  assert.match(conciergeApp, /const dayKey = selectedDayKey/);
  assert.match(calendarView, /range complete/);
});

test("a day agenda lists the day the grid counted, not yesterday's leftovers", () => {
  // A day window matches by interval overlap, so a multi-day event that started
  // earlier is live on this day too and sorts ahead of it. The grid counts an
  // event on the day it starts, so the agenda must filter to the same rule.
  assert.match(conciergeApp, /function eventsStartingOn/);
  assert.match(conciergeApp, /localDateKey\(item\.start_at\) === dayKey/);
  assert.match(
    conciergeApp,
    /const dayItems = eventsStartingOn\(collected, dayKey\);\s*setDayEvents\(dayItems\)/,
  );
  // ...and must not stop on a page that held only those leftovers.
  assert.match(conciergeApp, /MAX_CALENDAR_DAY_SKIP_PAGES/);
  assert.match(
    conciergeApp,
    /if \(cursor === null \|\| eventsStartingOn\(collected, dayKey\)\.length\) break/,
  );
});

test("the filter rail counts the summarized range while the calendar is open", () => {
  assert.match(
    conciergeApp,
    /resultCount=\{view === "calendar"\s*\?\s*summary\?\.total_event_count \?\? 0/,
  );
});

test("a cached summary paints the calendar before revalidation replaces it", () => {
  assert.match(
    conciergeApp,
    /readCatalogSummary\(tenantId, signature, start, end\)/,
  );
  assert.match(conciergeApp, /setSummary\(cached\.summary\)/);
  assert.match(
    conciergeApp,
    /writeCatalogSummary\(tenantId, signature, start, end, nextSummary\)/,
  );
  // A dropped session must not leave another account's ranges readable.
  assert.match(conciergeApp, /clearSession\(\);[\s\S]*clearCatalogCache\(\)/);
});

test("a range already read serves the grid without a request", () => {
  // Switching modes narrows or widens the window over a filter already read, so
  // the counts are in hand: issuing the range read again is what made every mode
  // switch a round trip.
  assert.match(
    conciergeApp,
    /if \(cached\?\.covered && !cached\.stale\) \{[\s\S]*setSummaryLoading\(false\);[\s\S]*return;/,
  );
});

test("the filter inventory never gates the counts the grid paints", () => {
  // The rail labels these counts "in range", so they follow the window — but the
  // grid must not wait on a request that costs three times its own.
  assert.match(conciergeApp, /if \(facetsSignature\.current === signature\) return;/);
  // Both are catalog-wide aggregations served from one pool. Issued together, the
  // inventory's seconds were added to the wait for the grid - the only one of the
  // two anybody is looking at - so the inventory is read after the counts.
  assert.match(conciergeApp, /const readInventory = \(\) => \{/);
  assert.match(
    conciergeApp,
    /setSummaryLoading\(false\);\s*setCatalogError\(null\);\s*readInventory\(\);\s*return;/,
  );
  assert.match(
    conciergeApp,
    /if \(generation === summaryGeneration\.current\) setSummaryLoading\(false\);\s*readInventory\(\);/,
  );
  // A failed read releases only its own claim, never a newer one's.
  assert.match(
    conciergeApp,
    /if \(facetsSignature\.current === signature\) facetsSignature\.current = null;/,
  );
});

test("a day already opened re-opens from memory", () => {
  assert.match(
    conciergeApp,
    /const cachedDay = readCatalogDayEvents\(tenantId, agendaSignature, dayKey\)/,
  );
  assert.match(conciergeApp, /setDayLoading\(false\);\n      return;/);
  assert.match(
    conciergeApp,
    /writeCatalogDayEvents\(tenantId, agendaSignature, dayKey, dayItems, cursor\)/,
  );
});

test("an agenda page asks for the page alone", () => {
  // Three whole-catalog facet aggregations cost more than the page itself, and
  // the agenda renders none of them.
  assert.match(apiClient, /query\.set\("include_facets", "false"\)/);
});

test("the paged agenda still guards duplicate cursors", () => {
  assert.match(conciergeApp, /loadingCursor\.current === nextCursor/);
  assert.match(conciergeApp, /loadingCursor\.current = cursor/);
});

test("entering an unbounded calendar view requests the active calendar range", () => {
  assert.match(
    conciergeApp,
    /nextCalendarMode = nextView === "calendar"[\s\S]*calendarModeForFilters[\s\S]*nextFilters = nextView === "calendar"[\s\S]*calendarRangeFilters/,
  );
  assert.match(
    conciergeApp,
    /view !== "calendar"[\s\S]*filters\.datePreset !== "all"[\s\S]*filters\.datePreset !== "source"[\s\S]*calendarRangeFilters/,
  );
});

test("an explicitly selected empty day remains selected", () => {
  assert.match(calendarView, /selectedKey && selectedKey >= startKey && selectedKey <= endKey/);
  assert.doesNotMatch(calendarView, /!byDate\.has\(selectedKey\)/);
  assert.match(calendarView, /selectedKey < localDateKey\(new Date\(\)\)/);
  assert.match(calendarView, /selectedHistorical \? "No retained events" : "A quiet day"/);
  assert.match(calendarView, /Nothing is scheduled for this day/);
});

test("calendar exposes Today and persisted week, month, and six-month modes", () => {
  assert.match(calendarView, />\s*Today\s*</);
  assert.match(calendarView, /\["week", "Week"\]/);
  assert.match(calendarView, /\["month", "Month"\]/);
  assert.match(calendarView, /\["six-months", "6 months"\]/);
  assert.match(calendarView, /CalendarWeekGrid/);
  assert.match(calendarView, /calendar-six-months/);
  assert.match(conciergeApp, /mode=\{calendarMode\}/);
  assert.match(conciergeApp, /onRangeChange=\{handleCalendarRangeChange\}/);
});

test("calendar restores custom links against the complete selected view range", () => {
  assert.match(conciergeApp, /function alignCalendarSnapshot/);
  assert.match(conciergeApp, /calendarRangeFilters\(snapshot\.filters, focusDate, snapshot\.calendarMode\)/);
  assert.match(conciergeApp, /alignCalendarSnapshot\(readConsumerHistorySnapshot/);
  assert.match(conciergeApp, /releaseHistorySnapshot\(alignCalendarSnapshot\(rawSnapshot\), profile\)/);
});

test("selecting a day marks it with an accent ring, not an inverted light fill", () => {
  // A white fill punched a hole through the dark grid and forced every topic
  // chip inside the cell onto a second set of colours.
  assert.doesNotMatch(styles, /\.calendar-grid button\.is-selected \{[^}]*background: var\(--ink\)/);
  assert.doesNotMatch(
    styles,
    /\.calendar-week-grid > button\.is-selected \{[^}]*background: var\(--ink\)/,
  );
  assert.match(
    styles,
    /\.calendar-grid button\.is-selected \{[^}]*box-shadow: inset 0 0 0 1px var\(--accent\)/,
  );
  assert.match(
    styles,
    /\.calendar-week-grid > button\.is-selected \{[^}]*box-shadow: inset 0 0 0 1px var\(--accent\)/,
  );
  // Nothing inside a selected cell may assume a light background any more.
  assert.doesNotMatch(styles, /is-selected[^{]*\{[^}]*color: rgba\(0, 0, 0/);
});

test("a compact cell is a fixed box its contents cannot escape", () => {
  // Six-month cells are under 30px wide. Numbered topic pills could not shrink
  // into that, so they spilled across day borders and stretched whole rows.
  assert.match(styles, /\.calendar-month\.is-compact \.calendar-grid \{[^}]*grid-auto-rows: 56px/);
  assert.match(
    styles,
    /\.calendar-month\.is-compact \.calendar-grid button \{[^}]*overflow: hidden/,
  );
  assert.match(styles, /\.calendar-cell__compact-counts \{[^}]*overflow: hidden/);
  // The date leads and never shrinks; the mix is tones, not numbers.
  assert.match(
    styles,
    /\.calendar-month\.is-compact \.calendar-cell__top > span \{[^}]*flex: 0 0 auto/,
  );
  // The tone is a mark, not a number: nothing is rendered inside it.
  assert.match(calendarView, /className="calendar-cell__compact-count"[\s\S]{0,400}?\/>/);
  assert.doesNotMatch(
    calendarView,
    /calendar-cell__compact-count"[\s\S]{0,400}?>\s*\{category\.eventCount\}/,
  );
  assert.match(calendarView, /title=\{`\$\{category\.label\}: \$\{category\.eventCount\}`\}/);
});

test("six-month mode renders two rows of three compact calendars", () => {
  assert.match(calendarView, /\[0, 1, 2, 3, 4, 5\]/);
  assert.match(styles, /\.calendar-six-months[\s\S]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(styles, /\.calendar-month\.is-compact:nth-child\(-n \+ 3\)/);
});

test("calendar views share a deterministic event-topic color system", () => {
  assert.equal(eventTopicTone("ai"), "cyan");
  assert.equal(eventTopicTone("technology"), "blue");
  assert.equal(eventTopicTone("music"), "rose");
  assert.equal(eventTopicTone("sports"), "green");
  assert.equal(eventTopicTone("unknown-future-topic"), "slate");
  assert.match(calendarView, /calendar-cell__compact-counts/);
  assert.match(calendarView, /data-topic-tone=\{eventTopicTone\(category\.topic\)\}/);
  assert.match(calendarView, /data-topic-tone=\{eventTopicTone\(event\.topics\?\.\[0\]\)\}/);
  assert.match(styles, /\[data-topic-tone="cyan"\]/);
  assert.match(styles, /\.calendar-cell__compact-count/);
});

test("calendar agenda explains colors and exposes persistent interactive topic filters", () => {
  assert.match(calendarView, /calendar-topic-key/);
  assert.match(calendarView, /COLOR KEY \+ FILTER/);
  assert.match(calendarView, /data-topic-tone=\{eventTopicTone\(topic\.topic\)\}/);
  assert.match(calendarView, /aria-pressed=\{active\}/);
  assert.match(calendarView, /onClick=\{\(\) => onTopicSelect\(topic\.topic\)\}/);
  assert.match(calendarView, /onClick=\{onTopicsClear\}/);
  assert.match(calendarView, /calendarSummaryLegendTopics\(summary, topics, activeTopics\)/);
  assert.match(calendarView, /Counts reflect the visible events/);
  assert.match(styles, /\.calendar-topic-key[\s\S]*border-bottom: 1px solid var\(--line\)/);
  assert.match(styles, /\.calendar-topic-key ul[\s\S]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(calendarView, /calendar-layout[\s\S]*calendar-agenda[\s\S]*calendar-topic-key[\s\S]*<EventList/);
  assert.match(conciergeApp, /activeTopics=\{filters\.topics\}/);
  assert.match(conciergeApp, /topics=\{topicFacets\}/);
  assert.match(conciergeApp, /filters\.topics\.filter\(\(value\) => value !== topic\)/);
  assert.match(conciergeApp, /view === "calendar" \? "calendar" : "events"/);
});

test("calendar cells expose category labels instead of dot-only status", () => {
  assert.match(calendarView, /calendar-cell__categories/);
  assert.match(calendarView, /category\.label/);
  assert.match(calendarView, /category\.eventCount/);
  assert.match(calendarView, /visibleCategories = categories\.slice\(0, 4\)/);
  assert.match(calendarView, /more categor/);
  assert.match(calendarView, /aria-label=\{`\$\{dateLabel\}/);
  assert.match(styles, /\.calendar-cell__category/);
  assert.match(styles, /grid-auto-rows: minmax\(164px, auto\)/);
  assert.match(styles, /\.calendar-cell__more/);
  assert.doesNotMatch(styles, /\.calendar-grid button\.has-events::after/);
});

test("a week is named by the dates it covers, not by an ordinal", () => {
  // The rail is a way into that week, so it says where it leads. "Week 3" would
  // make the reader count rows to find out.
  assert.equal(
    calendarWeekSpanLabel(new Date(2026, 7, 16), "en-US"),
    "Aug 16 – 22",
  );
  // A week that crosses a month names both, or the second half reads as August.
  assert.equal(
    calendarWeekSpanLabel(new Date(2026, 7, 30), "en-US"),
    "Aug 30 – Sep 5",
  );
  // ...including across a year.
  assert.equal(
    calendarWeekSpanLabel(new Date(2026, 11, 27), "en-US"),
    "Dec 27 – Jan 2",
  );
});

test("an overview drills into the part of it being looked at", () => {
  // Six months of two-digit cells and a month of dense ones are both overviews.
  // Their parts open directly rather than making the reader switch mode and then
  // navigate back to where they already were.
  assert.match(calendarView, /const openRange = \(nextAnchor: Date, nextMode: CalendarMode\) => \{/);
  assert.match(calendarView, /onMonthSelect=\{\(nextMonth\) => openRange\(nextMonth, "month"\)\}/);
  assert.match(calendarView, /onWeekSelect=\{\(weekStart\) => openRange\(weekStart, "week"\)\}/);
  // The month heading is the control in the six-month view...
  assert.match(calendarView, /className="calendar-month__open"[\s\S]*?aria-label=\{`Open \$\{monthLabel\}`\}/);
  // ...and the rail is the control in the month view.
  assert.match(
    calendarView,
    /className="calendar-week-rail"[\s\S]*?aria-label=\{`Open the week of \$\{calendarWeekSpanLabel\(week\.start\)\}`\}/,
  );
  // The rail is the same control in both grids; only its label shortens, because
  // a six-month cell is about thirty pixels wide.
  assert.match(calendarView, /const railed = Boolean\(onWeekSelect\);/);
  // A number in that gutter read as one more column of dates; a chevron cannot.
  assert.match(
    calendarView,
    /\{compact\s*\?\s*<ChevronRight aria-hidden="true" \/>\s*:\s*calendarWeekSpanLabel\(week\.start\)\}/,
  );
  // The span the rail leads to stays reachable where it costs no width.
  assert.match(calendarView, /title=\{`Open the week of \$\{calendarWeekSpanLabel\(week\.start\)\}`\}/);
});

test("the week rail shares the grid so a growing row carries its label", () => {
  // A rail beside the grid would drift the moment a row grew to fit a busy day.
  assert.match(styles, /\.calendar-month\.has-week-rail \.calendar-weekdays,\s*\.calendar-month\.has-week-rail \.calendar-grid \{\s*grid-template-columns: 76px repeat\(7, minmax\(0, 1fr\)\);/);
  // Eight columns means the outer-border rules count by eight.
  assert.match(styles, /\.calendar-month\.has-week-rail \.calendar-grid button:nth-child\(8n\)/);
  assert.match(styles, /\.calendar-month\.has-week-rail \.calendar-grid button:nth-last-child\(-n \+ 8\)/);
  // ...and the seven-column rules must no longer apply to a railed grid.
  assert.match(styles, /\.calendar-month:not\(\.has-week-rail\) \.calendar-grid button:nth-child\(7n\)/);
  // The rail is itself a `.calendar-grid button`, so its own rules must out-specify that.
  assert.match(styles, /\.calendar-grid button\.calendar-week-rail \{/);
  // The compact grid is eight columns too, so its edge rules count by eight.
  assert.match(styles, /\.calendar-month\.is-compact\.has-week-rail \.calendar-grid button:nth-child\(8n\)/);
  assert.match(styles, /\.calendar-month\.is-compact:not\(\.has-week-rail\) \.calendar-grid button:nth-child\(7n\)/);
});

test("opening the key extends the panel rather than scrolling a letterbox", () => {
  // Five rows leave the agenda below in view; opening it grows the list to hold
  // the rest, and only a catalog longer than that scrolls at all.
  assert.match(calendarView, /const CALENDAR_LEGEND_ROWS = 5;/);
  assert.match(
    calendarView,
    /visibleLegendTopics\.slice\(0, CALENDAR_LEGEND_ROWS \* 2\)/,
  );
  assert.match(
    styles,
    /\.calendar-topic-key__list \{[\s\S]*?max-height: 179px;[\s\S]*?overflow-y: auto;/,
  );
  assert.match(
    styles,
    /\.calendar-topic-key\.is-expanded \.calendar-topic-key__list \{\s*max-height: min\(52vh, 460px\);/,
  );
  // One control toggles both ways; there is no separate dismiss.
  assert.match(calendarView, /setLegendExpanded\(\(current\) => !current\)/);
  assert.match(calendarView, /legendExpanded \? "Show fewer"/);
  // The control must carry its own styling, not fall back to a bare button.
  assert.match(styles, /\.calendar-topic-key__more \{[\s\S]*?border: 1px dashed var\(--line-strong\);/);
  assert.doesNotMatch(calendarView, /showModal|<dialog/);
  assert.doesNotMatch(styles, /calendar-topic-dialog/);
});

test("a filter change never empties what is on screen", () => {
  // THE fundamental blink: the view cleared its rendered data at the START of a
  // fetch instead of replacing it at the END, so every filter change tore the
  // grid, the agenda and the key down to nothing and rebuilt them a second later.
  assert.doesNotMatch(
    conciergeApp,
    /const cached = start && end[\s\S]{0,400}?\} else \{\s*setSummary\(null\);/,
  );
  assert.match(conciergeApp, /Deliberately NOT cleared/);
  // The agenda keeps its day while the next one is read.
  assert.doesNotMatch(conciergeApp, /setDayLoading\(true\);\s*setDayEvents\(\[\]\);/);
  // A week column keeps what it was showing until its own read lands.
  assert.match(
    conciergeApp,
    /setDayPreviews\(\(current\) => new Map\(\[\.\.\.current, \.\.\.seeded\]\)\)/,
  );
  // Held content is dimmed, so nothing claims the old numbers are current.
  assert.match(calendarView, /const restating = !counted && Boolean\(summary\);/);
  // A failed read says so; it does not take the page away.
  assert.match(conciergeApp, /not a reason to take the page/);
  assert.doesNotMatch(
    conciergeApp,
    /setCatalogError\(readableError\(error\)\);\s*if \(!cached\) \{\s*setSummary\(null\);/,
  );
  assert.match(calendarView, /calendar-grid-shell\$\{restating \? " is-restating" : ""\}/);
  assert.match(styles, /\.calendar-grid-shell\.is-restating \.calendar-grid,/);
  assert.match(styles, /\.calendar-agenda\.is-restating \.event-list \{[\s\S]*?opacity: 0\.5;/);
});

test("the key holds its shape while a range reloads", () => {
  // Choosing a type re-reads the range, and for that moment there are no counts:
  // the key would collapse to the one type picked and then repopulate. That
  // emptying and refilling is the flicker.
  assert.match(
    calendarView,
    /if \(!summary \|\| !summaryCovered\) return;\s*setLegendTopics\(freshLegendTopics\);/,
  );
  assert.match(calendarView, /const legendPending = Boolean\(summary\) && !summaryCovered;/);
  assert.match(calendarView, /legendPending \? "is-pending" : ""/);
  // One rule dims the key, the grid and the agenda: they all restate one answer.
  assert.match(styles, /\.calendar-topic-key\.is-pending \.calendar-topic-key__list,/);
});

test("the key searches from the start and multi-selects the way the filters do", () => {
  // Naming a type is the fastest way to reach one, so the search renders with the
  // key rather than behind anything.
  assert.match(calendarView, /className="calendar-topic-key__search"/);
  assert.match(
    calendarView,
    /const needle = legendTerm\.trim\(\)\.toLocaleLowerCase\(\);[\s\S]*?topic\.label\.toLocaleLowerCase\(\)\.includes\(needle\)/,
  );
  // Taking a type hands the search back with its term SELECTED, whether it was
  // clicked or entered: the word that found the type stays legible, and typing
  // over it replaces it. Two ways of choosing that behave differently was the bug.
  assert.match(
    calendarView,
    /const chooseLegendTopic = \(topic: string\) => \{[\s\S]*?onTopicSelect\(topic\);[\s\S]*?legendSearch\.current\?\.focus\(\);[\s\S]*?legendSearch\.current\?\.select\(\)/,
  );
  assert.match(calendarView, /onTopicSelect=\{chooseLegendTopic\}/);
  assert.match(calendarView, /chooseLegendTopic\(top\.topic\);/);
  // A term left untouched must not be cleared, so browsing is not interrupted.
  assert.match(calendarView, /if \(!legendTerm\) return;/);
  // The picks are restated as a row that can be unpicked without hunting.
  assert.match(calendarView, /className="calendar-topic-key__picks"/);
  assert.match(calendarView, /aria-label=\{`Remove \$\{picked\?\.label \?\? topic\}`\}/);
  // Never a dialog over the page.
  assert.doesNotMatch(calendarView, /showModal|<dialog/);
  assert.doesNotMatch(styles, /calendar-topic-dialog/);
});

test("the open day follows the events when a filter moves them", () => {
  // Narrowing the types re-answers the range, and the day being read can come
  // back empty while the matches sit a few cells away. The agenda then said
  // "a quiet day" about a calendar plainly holding events, which reads as the
  // filter having found nothing.
  assert.match(
    calendarView,
    /if \(\(byDate\.get\(selectedKey\)\?\.event_count \?\? 0\) > 0\) return;/,
  );
  assert.match(
    calendarView,
    /const firstWithEvents = \[\.\.\.byDate\.keys\(\)\]\.sort\(\)\.find\(\(key\) => \([\s\S]*?\(byDate\.get\(key\)\?\.event_count \?\? 0\) > 0/,
  );
  // Guarded on the counts themselves changing, so a deliberately chosen empty
  // day stays chosen: a click re-answers nothing, so this cannot fire on one.
  assert.match(calendarView, /if \(countedDays\.current === byDate\) return;\s*countedDays\.current = byDate;/);
  // ...and it never acts on counts that do not describe the visible range.
  assert.match(calendarView, /if \(!counted \|\| !selectedKey\) return;/);
});
