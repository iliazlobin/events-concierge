import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { catalogPriceMaxCents, getCatalogPage } from "../lib/api.ts";

const filterBar = readFileSync(
  new URL("../components/filter-bar.tsx", import.meta.url),
  "utf8",
);
const dateRange = readFileSync(
  new URL("../components/date-range-picker.tsx", import.meta.url),
  "utf8",
);
const chipEditor = readFileSync(
  new URL("../components/filter-chip-editor.tsx", import.meta.url),
  "utf8",
);
const chipPopover = readFileSync(
  new URL("../components/filter-chip-popover.tsx", import.meta.url),
  "utf8",
);
const priceEditor = readFileSync(
  new URL("../components/price-filter-editor.tsx", import.meta.url),
  "utf8",
);
const filterSuggestions = readFileSync(
  new URL("../lib/filter-suggestions.ts", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);

const FILTERS = {
  query: "",
  sort: "latest",
  datePreset: "week",
  customStart: "",
  customEnd: "",
  dateRanges: [],
  sourceKeys: [],
  city: "sanfrancisco",
  cities: ["sanfrancisco", "santamonica"],
  locationScopes: ["manhattan"],
  topics: ["sports", "volleyball"],
  price: "any",
  priceComparison: "at-most",
  priceMinDollars: "",
  priceMaxDollars: "25.50",
  availability: "sold_out",
};

test("price dollars convert to a bounded exact-cent ceiling", () => {
  assert.equal(catalogPriceMaxCents("25.50"), 2550);
  assert.equal(catalogPriceMaxCents("7.5"), 750);
  assert.equal(catalogPriceMaxCents("0"), null);
  assert.equal(catalogPriceMaxCents("20.999"), null);
  assert.equal(catalogPriceMaxCents("not money"), null);
});

test("catalog URL repeats additive locations and sends the exact price ceiling", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  globalThis.fetch = async (input) => {
    requested = String(input);
    return new Response(JSON.stringify({
      items: [],
      next_cursor: null,
      providers: [],
      topic_facets: [],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await getCatalogPage("tenant", FILTERS);
  } finally {
    globalThis.fetch = originalFetch;
  }
  const url = new URL(requested, "http://example.test");
  assert.deepEqual(url.searchParams.getAll("city"), ["sanfrancisco", "santamonica"]);
  assert.deepEqual(url.searchParams.getAll("location_scope"), ["manhattan"]);
  assert.deepEqual(url.searchParams.getAll("topic"), ["sports", "volleyball"]);
  assert.equal(url.searchParams.get("price"), null);
  assert.equal(url.searchParams.get("price_max_cents"), "2550");
  assert.equal(url.searchParams.get("availability"), "sold_out");
  assert.equal(url.searchParams.get("sort"), "latest");
});

test("catalog URL repeats additive date ranges and omits legacy singular bounds", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  globalThis.fetch = async (input) => {
    requested = String(input);
    return new Response(JSON.stringify({
      items: [],
      next_cursor: null,
      providers: [],
      topic_facets: [],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await getCatalogPage("tenant", {
      ...FILTERS,
      datePreset: "custom",
      dateRanges: [
        { id: "a", start: "2026-08-01", end: "2026-08-02" },
        { id: "b", start: "2026-08-08", end: "2026-08-09" },
        { id: "duplicate", start: "2026-08-01", end: "2026-08-02" },
      ],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
  const url = new URL(requested, "http://example.test");
  const ranges = url.searchParams.getAll("date_range");
  assert.equal(ranges.length, 2);
  for (const [serialized, expectedStart, expectedEnd] of [
    [ranges[0], [2026, 7, 1], [2026, 7, 3]],
    [ranges[1], [2026, 7, 8], [2026, 7, 10]],
  ]) {
    const [startValue, endValue] = serialized.split("..");
    const start = new Date(startValue);
    const end = new Date(endValue);
    assert.deepEqual(
      [start.getFullYear(), start.getMonth(), start.getDate(), start.getHours()],
      [...expectedStart, 0],
    );
    assert.deepEqual(
      [end.getFullYear(), end.getMonth(), end.getDate(), end.getHours()],
      [...expectedEnd, 0],
    );
  }
  assert.equal(url.searchParams.get("starts_after"), null);
  assert.equal(url.searchParams.get("starts_before"), null);
});

test("catalog URL retains legacy preset bounds when no additive range is selected", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  globalThis.fetch = async (input) => {
    requested = String(input);
    return new Response(JSON.stringify({
      items: [],
      next_cursor: null,
      providers: [],
      topic_facets: [],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await getCatalogPage("tenant", { ...FILTERS, dateRanges: [] });
  } finally {
    globalThis.fetch = originalFetch;
  }
  const url = new URL(requested, "http://example.test");
  assert.equal(url.searchParams.getAll("date_range").length, 0);
  assert.ok(url.searchParams.get("starts_after"));
  assert.ok(url.searchParams.get("starts_before"));
});

test("the composer keeps typed place and topic filters additive", () => {
  assert.match(
    filterBar,
    /filterSmartFilterSuggestionsByContext\(candidates, effectiveComposerContext\)/,
  );
  // The edit popover no longer carries its own "Add another …" link: the strip already
  // ends in one dashed chip per filter kind, and typing the kind opens the same composer.
  assert.doesNotMatch(filterBar, /addLabel="Add another/);
  assert.doesNotMatch(chipEditor, /onAdd/);
  assert.match(filterBar, /interpretCatalogFilterExpression/);
  assert.match(filterBar, /commitFilterExpression\(\)/);
  assert.match(filterBar, /expression\.appliedKinds\.length >= 2/);
  assert.match(filterBar, /next\.cities = \[\.\.\.next\.cities, suggestion\.value\]/);
  assert.match(
    filterBar,
    /next\.locationScopes = \[\.\.\.next\.locationScopes, suggestion\.value\]/,
  );
  assert.match(filterBar, /next\.topics = \[\.\.\.next\.topics, suggestion\.value\]/);
  assert.match(filterBar, /filters\.topics\.map\(\(topic\) => \(/);
  assert.match(filterBar, /removeTopic\(topic\)/);
});

test("category-first typing opens a scoped composer without becoming event search", () => {
  assert.match(filterBar, /parseSmartFilterComposerQuery\(query\)/);
  assert.match(filterBar, /composerContext \? query : typedComposer\?\.term \?\? query/);
  assert.match(filterBar, /getPopularSmartFilterSuggestions\(/);
  assert.match(filterBar, /if \(composerOnly \|\| typedComposer\) return undefined/);
  assert.match(filterBar, /effectiveComposerContext[\s\S]*filterSmartFilterSuggestionsByContext/);
  assert.match(filterBar, /onChange\(next, "push"\)/);
  assert.match(filterBar, /searchInputRef\.current\?\.focus\(\)/);
  assert.match(filterBar, /setSuggestionsSuppressed\(true\)/);
  assert.match(filterBar, /searchFocused && !suggestionsSuppressed/);
  assert.match(filterBar, /type place, topic, source, date, price, or registration/);
  assert.doesNotMatch(
    readFileSync(new URL("../components/concierge-app.tsx", import.meta.url), "utf8"),
    /next\.sourceKey && next\.sourceKey !== filters\.sourceKey/,
  );
});

test("date filtering supports additive editable ranges and active filter chips", () => {
  assert.doesNotMatch(filterBar, /type="date"/);
  assert.doesNotMatch(filterBar, /date-switcher/);
  assert.match(filterBar, /getStarterFilterSuggestions/);
  assert.match(chipPopover, /active-filter-chip/);
  assert.equal(filterBar.match(/<DateRangePicker/g)?.length, 2);
  assert.match(filterBar, /variant="chip"/);
  assert.match(filterBar, /variant="add"/);
  assert.match(filterBar, /onApply=\{\(start, end\) => applyDateRange\("new", start, end\)\}/);
  assert.doesNotMatch(filterBar, /requestDatePicker/);
  assert.match(dateRange, /role="dialog"/);
  assert.match(dateRange, /variant\?: "control" \| "chip" \| "add"/);
  assert.match(dateRange, /setDraftStart/);
  assert.match(dateRange, /setDraftEnd/);
  assert.match(dateRange, /Matches either date range/);
  assert.match(dateRange, /Remove range/);
  // The second endpoint completes the range, so it applies itself; there is no button to repeat.
  assert.match(dateRange, /setSelectingEnd\(false\);\s*\n\s*onApply\(start, end\);\s*\n\s*setOpen\(false\);/);
  assert.doesNotMatch(dateRange, /Add range/);
  assert.doesNotMatch(dateRange, /is-primary/);
});

test("a named window survives being applied, from wherever it was chosen", () => {
  // Every date suggestion used to be resolved to the days it meant today and pinned as a range.
  // That made a starting point, a typed phrase and a kept selection all go stale the moment the
  // week turned, and left the picker with no window lit -- the filter no longer said which one
  // it was. Only a genuinely custom suggestion pins.
  const applied = filterBar.match(
    /if \(suggestion\.kind === "date"\) \{[\s\S]*?\n  \}\n  return next;/,
  )?.[0];
  assert.ok(applied, "one place should own applying a date suggestion");
  assert.match(applied, /if \(suggestion\.value !== "custom"\) \{\s*\n\s*next\.datePreset = suggestion\.value;/);
  assert.match(applied, /next\.dateRanges = \[\];\s*\n\s*return next;/);
  assert.match(applied, /semanticDateRangeKeys\(\s*\n\s*"custom",/);
  // And it reads as active by name, not by the dates it resolves to: a pinned range covering
  // this week is a different filter from "this week".
  assert.match(
    filterBar,
    /return !dateRanges\.length && suggestion\.value === filters\.datePreset;/,
  );
});

test("a rolling window is named on the chip, and still says which dates it means", () => {
  // This reverses an earlier rule that the chip must always spell out dates. Spelling them out
  // made "this week" and a range pinned to this week indistinguishable -- which is precisely
  // what a kept selection has to distinguish, since one of the two goes stale.
  assert.match(filterBar, /summary=\{rolling \?\? explicitRange\}/);
  // The dates are not lost, only moved: hover carries them, and the calendar shows them.
  assert.match(
    filterBar,
    /summaryTitle=\{rolling \? `\$\{rolling\} · \$\{explicitRange\}` : undefined\}/,
  );
  assert.match(dateRange, /title=\{summaryTitle\}/);
  // Only the chip standing in for the whole date filter can be rolling; an additive range is
  // two fixed dates by construction.
  assert.match(filterBar, /range\.legacy \? rollingWindowLabel\(filters\.datePreset\) : null/);
  // Swapping a pinned range for a rolling window is the move worth offering, so the windows
  // are reachable from a single date chip whether or not it is pinned -- and hidden once
  // several ranges are in play, where choosing one would silently drop the others.
  assert.match(filterBar, /const swappableForWindow = dateChips\.length === 1;/);
  assert.match(filterBar, /onPreset=\{swappableForWindow \? applyDatePreset : undefined\}/);
  assert.match(filterBar, /const explicitRange = friendlyRange\(range\.start, range\.end\)/);
});

test("every active date chip has an accessible remove control", () => {
  assert.doesNotMatch(
    filterBar,
    /const removable\s*=\s*!range\.legacy/,
    "the implicit active date window is removable just like place and topic filters",
  );
  assert.match(
    filterBar,
    /variant="chip"[\s\S]{0,360}removable(?:=\{true\})?[\s\S]{0,180}removeLabel=/,
  );
  assert.match(dateRange, /aria-label=\{removeLabel\}/);
  assert.match(dateRange, /<X aria-hidden="true" \/>/);
});

test("Reset restores the truly unfiltered default state", () => {
  const resetBody = filterBar.match(
    /const reset = \(\) => \{[\s\S]*?\n  \};/,
  )?.[0];
  assert.ok(resetBody, "FilterBar should retain one explicit reset transaction");
  assert.match(resetBody, /setQuery\(""\)/);
  assert.match(resetBody, /onChange\(emptyCatalogFilters\(\), "push"\)/);
  assert.doesNotMatch(resetBody, /DEFAULT_CATALOG_CITY/);
  assert.doesNotMatch(filterBar, /active-filter-strip__label/);
});

test("price type and amount flow through the same smart composer and chip", () => {
  assert.match(filterBar, /suggestion\.maximumDollars !== undefined/);
  assert.match(filterBar, /next\.priceMaxDollars = suggestion\.maximumDollars/);
  assert.match(
    filterBar,
    /next\.priceComparison = "at-most"/,
    "a suggested ceiling is the at-most comparison, not a bare number",
  );
  assert.match(priceEditor, /chipLabel="Price"/);
  assert.match(chipPopover, /<small>\{chipLabel\}<\/small>/);
  assert.match(filterBar, /priceLabel\(filters\)/);
  assert.match(
    priceEditor,
    /comparison: takesAmount \? draft\.comparison : "any"/,
    "a category that cannot carry an amount must drop it when saved",
  );
  assert.doesNotMatch(filterBar, /<PriceFilterControl/);
});

test("price offers a comparison rather than only a ceiling", () => {
  for (const marker of ['value: "at-most"', 'value: "at-least"', 'value: "exactly"', 'value: "between"']) {
    assert.ok(priceEditor.includes(marker), `${marker} should be offered`);
  }
  // An incomplete comparison never applies -- what the disabled Save used to express.
  const complete = priceEditor.match(/function selectionIsComplete[\s\S]*?\n\}/)?.[0];
  assert.ok(complete, "the price editor should own one completeness rule");
  assert.match(complete, /comparison === "at-most"\) return amountIsUsable\(maximum\)/);
  assert.match(complete, /Number\(minimum\) <= Number\(maximum\)/);
  assert.match(priceEditor, /if \(!selectionIsComplete\(draft\)\) return;/);
  // Applying live would rewrite the chip under its own open popover, so it commits on leave.
  assert.match(priceEditor, /onDismiss=\{commitDraft\}/);
  assert.doesNotMatch(priceEditor, /setTimeout/);
  assert.doesNotMatch(priceEditor, /confirmLabel/);
  // Free and unlisted cannot carry an amount at all.
  assert.match(priceEditor, /return price !== "free" && price !== "unknown"/);
  const label = filterBar.match(/function priceAmountLabel[\s\S]*?\n\}/)?.[0];
  assert.ok(label, "the chip should render the comparison it holds");
  for (const shape of ["`≤ \\$${maximum}`", "`≥ \\$${minimum}`", "`= \\$${minimum}`"]) {
    assert.ok(label.includes(shape.replace("\\$", "$")), `${shape} should be a chip label`);
  }
});

test("the client sends the bounds the API defines rather than a bare ceiling", () => {
  const api = readFileSync(new URL("../lib/api.ts", import.meta.url), "utf8");
  const bounds = api.match(/export function catalogPriceBoundCents[\s\S]*?\n\}/)?.[0];
  assert.ok(bounds, "one helper should resolve a comparison into two bounds");
  assert.match(bounds, /comparison === "at-most"\) return \{ min: null, max: maximum \}/);
  assert.match(bounds, /comparison === "exactly"/);
  assert.match(bounds, /min: minimum, max: minimum/);
  // A floor cannot describe an event with no price, matching the server rule.
  assert.match(bounds, /filters\.price !== "free" && filters\.price !== "unknown"/);
  assert.match(api, /query\.set\("price_min_cents", String\(bounds\.min\)\)/);
  assert.match(api, /query\.append\("source_key", sourceKey\)/);
});

test("search composer and active chips are the only persistent filter surface", () => {
  assert.match(filterBar, /role="combobox"/);
  assert.match(filterBar, /aria-controls="smart-filter-suggestions"/);
  assert.doesNotMatch(filterBar, /className="filter-composer-toggle"/);
  assert.doesNotMatch(filterBar, />\s*Add filter\s*<\/button>/);
  assert.match(filterBar, /className="active-filter-strip"/);
  assert.match(chipPopover, /className="active-filter-chip__remove"/);
  assert.match(filterBar, /const MAX_DATE_RANGES = 8/);
  assert.doesNotMatch(filterBar, /filtersOpen/);
  assert.doesNotMatch(filterBar, /catalog-filter-panel/);
  assert.doesNotMatch(filterBar, /<CompactCombobox/);
  assert.doesNotMatch(filterBar, /<LocationMultiCombobox/);
  assert.doesNotMatch(filterBar, /<TopicMultiCombobox/);
  assert.doesNotMatch(styles, /\.filter-rail(?:__controls)?\s*\{/);
  assert.match(styles, /\.active-filter-strip\s*\{/);
});

test("registration availability is an editable, removable filter chip", () => {
  assert.match(filterBar, /summary="Add registration"/);
  assert.match(filterBar, /chipLabel="Registration"/);
  assert.match(filterBar, /REGISTRATION_FILTER_OPTIONS\.map/);
  assert.match(filterBar, /onRemove=\{clearAvailability\}/);
});

test("empty-search starting points use compact suggestion rows", () => {
  const starterButtonRule = styles.match(
    /\.filter-suggestions\.is-starters button\s*\{[^}]*\}/s,
  )?.[0];
  assert.ok(starterButtonRule, "starter suggestion button styles should be present");
  assert.doesNotMatch(starterButtonRule, /min-height:\s*66px/);
});

test("every active filter chip edits its own value in place", () => {
  for (const marker of [
    'chipLabel="Source"',
    'title="Edit source"',
    'chipLabel="Place"',
    'title="Edit place"',
    'chipLabel="Topic"',
    'title="Edit topic"',
    'title="Edit price"',
  ]) {
    assert.ok(filterBar.includes(marker), `${marker} should reach a chip editor`);
  }
  assert.equal(
    filterBar.match(/<FilterChipEditor$/gm)?.length
      - filterBar.match(/<FilterChipEditor\n\s+variant="add"/g)?.length,
    5,
    "source, area, city, topic, and availability chips are list-editable",
  );
  assert.equal(
    filterBar.match(/<PriceFilterEditor$/gm)?.length,
    2,
    "price is editable as a chip and startable from the strip",
  );
  assert.match(chipPopover, /role="dialog"/);
  assert.match(chipEditor, /role="listbox"/);
  assert.match(chipEditor, /aria-selected=\{draftIds\.includes\(option\.id\)\}/);
  assert.match(chipPopover, /aria-label=\{removeLabel\}/);
  // A single pick is a complete answer, so it applies as it is made and dismisses.
  assert.match(chipEditor, /setDraftIds\(selectedIds\)/);
  assert.match(chipEditor, /onApply\(\[optionId\]\);\s*\n\s*close\(\);/);
  // Leaving is what commits a popover that stayed open, and it must be a no-op when
  // nothing was picked.
  assert.match(chipPopover, /const leave = \(\) => \{\s*\n\s*onDismiss\?\.\(\);/);
  assert.match(chipEditor, /if \(!multiple \|\| !draftIds\.length\) return;/);
  assert.doesNotMatch(chipEditor, /confirmLabel/);
  assert.doesNotMatch(filterBar, /confirmLabel/);
  // The primary footer control is opt-in now, and no filter opts in.
  assert.match(chipPopover, /const confirmable = confirmLabel !== undefined && Boolean\(onConfirm\);/);
  assert.doesNotMatch(filterBar, /confirmLabel/);
  // Escape is handled on the popover, so it abandons the draft from a focused
  // option row, not only from the text fields.
  assert.match(
    chipPopover,
    /role="dialog"[\s\S]{0,200}onKeyDown=\{\(event\) => \{\s*\n\s*if \(event\.key !== "Escape"\) return;/,
  );
  assert.match(chipPopover, /triggerRef\.current\?\.focus\(\)/);
  assert.match(chipPopover, /if \(!control\.contains\(document\.activeElement\)\) leave\(\)/);
});

test("editing a place chip can cross between cities and areas", () => {
  const body = filterBar.match(/const applyPlaceEdit = [\s\S]*?\n  \};/)?.[0];
  assert.ok(body, "FilterBar should own one place-edit transaction");
  assert.match(body, /parsePlaceOptionId\(optionId\)/);
  assert.match(
    body,
    /cities = cities\.map\(\(city\) => \(city === current\.value \? next\.value : city\)\)/,
    "swapping one city for another keeps its position in the strip",
  );
  assert.match(body, /locationScopes = \[\.\.\.locationScopes, next\.value\]/);
  assert.match(body, /cities = \[\.\.\.new Set\(cities\)\]/);
  assert.match(body, /locationScopes = \[\.\.\.new Set\(locationScopes\)\]/);
  assert.match(body, /city: cities\[0\] \?\? ""/);
});

test("editing a topic chip replaces only that topic", () => {
  const body = filterBar.match(/const applyTopicEdit = [\s\S]*?\n  \};/)?.[0];
  assert.ok(body, "FilterBar should own one topic-edit transaction");
  assert.match(body, /filters\.topics\.map\(\(topic\) => \(\n\s*topic === current \? next : topic\n\s*\)\)/);
  assert.match(body, /new Set\(/, "swapping onto an already active topic must not duplicate it");
});

test("the price editor and the search composer name prices the same way", () => {
  assert.match(priceEditor, /PRICE_FILTER_OPTIONS\.map/);
  assert.match(
    filterSuggestions,
    /export const PRICE_FILTER_OPTIONS[\s\S]*?PRICE_SUGGESTIONS\.map/,
    "the chip editor must not restate the price vocabulary",
  );
});

test("every filter kind can be started from the strip, not only dates", () => {
  for (const marker of [
    'summary="Add source"',
    'summary="Add place"',
    'summary="Add topic"',
    'summary="Add price"',
    'summary="Add registration"',
    'summary="Add dates"',
  ]) {
    assert.ok(filterBar.includes(marker), `${marker} should be offered in the strip`);
  }
  assert.match(chipPopover, /variant\?: "chip" \| "add"/);
  assert.match(chipPopover, /"active-filter-add filter-chip-editor__trigger--add"/);
  // Adding is a distinct transaction from editing: no value is preselected and
  // the left footer control cancels instead of removing.
  assert.match(chipEditor, /selectedIds = \[\]/);
  assert.match(chipPopover, /onRemove\?\.\(\)/);
  assert.equal(filterBar.match(/<FilterChipEditor\n\s+variant="add"/g)?.length, 4);
  // Nothing is staged in a popover that starts a new filter, so there is nothing to cancel:
  // it has no footer at all, and leaving it is how you leave.
  assert.doesNotMatch(filterBar, /clearActionLabel="Cancel"/);
  assert.doesNotMatch(dateRange, /Cancel/);
  assert.match(chipPopover, /const clearable = Boolean\(onRemove && clearActionLabel\);/);
  assert.match(chipPopover, /\{clearable \|\| confirmable \? \(\s*\n\s*<footer>/);
  // Price is the one filter that cannot be several values, so its add control
  // appears only while it is unset.
  assert.match(filterBar, /\{priceIsActive\(filters\) \? null : \(/);
});

test("adding a filter cannot duplicate a chip or exceed a server limit", () => {
  assert.match(filterBar, /const MAX_CITY_FILTERS = 20/);
  assert.match(filterBar, /const MAX_TOPIC_FILTERS = 12/);
  const places = filterBar.match(/const placeAddOptions = [\s\S]*?\n  \}, \[[^\]]*\]\);/)?.[0];
  assert.ok(places, "FilterBar should own one add-place option set");
  assert.match(places, /!active\.has\(option\.id\)/);
  assert.match(places, /cityCapReached && option\.id\.startsWith\("city:"\)/);
  const topics = filterBar.match(/const topicAddOptions = [\s\S]*?\n  \}, \[[^\]]*\]\);/)?.[0];
  assert.ok(topics, "FilterBar should own one add-topic option set");
  assert.match(topics, /filters\.topics\.length >= MAX_TOPIC_FILTERS\) return \[\]/);
  assert.match(filterBar, /disabled=\{!placeAddOptions\.length\}/);
  assert.match(filterBar, /disabled=\{!topicAddOptions\.length\}/);
  // Places are an OR on the server and topics are an AND; the hints must not swap.
  assert.match(filterBar, /hint="Matches any selected place"/);
  assert.match(filterBar, /hint="Keeps events carrying every topic"/);
});

test("adding places or topics accepts several picks at once", () => {
  // Only the filters the server can express as a repeated parameter are
  // multi-select: source_key and price are scalar in the API and the SQL.
  assert.equal(filterBar.match(/^\s+multiple$/gm)?.length, 3);
  assert.match(filterBar, /options=\{placeAddOptions\}\n\s+multiple$/m);
  assert.match(filterBar, /options=\{topicAddOptions\}\n\s+multiple$/m);
  assert.match(filterBar, /options=\{sourceAddOptions\}\n\s+multiple$/m);
  // Applying each pick as it lands widens the strip, which shifts the trigger this popover
  // is anchored to -- the list would slide out from under the pointer. So picks are held
  // and applied in one transaction on leave.
  assert.match(chipEditor, /onDismiss=\{commitPending\}/);
  assert.match(chipEditor, /onApply\(draftIds\);\s*\n\s*setDraftIds\(\[\]\);/);
  assert.match(chipEditor, /filter-chip-editor__picks/);
  assert.match(chipEditor, /aria-multiselectable=\{multiple \|\| undefined\}/);
  // A pick toggles in a multi-select and replaces in a single-select.
  const choose = chipEditor.match(/const choose = [\s\S]*?\n  \};/)?.[0];
  assert.ok(choose, "the editor should own one selection transaction");
  assert.match(choose, /if \(!multiple\) \{\n\s+setDraftIds\(\[optionId\]\);/);
  assert.match(choose, /current\.filter\(\(candidate\) => candidate !== optionId\)/);
  // Enter takes the top match, which is the same commitment a click is.
  assert.match(chipEditor, /choose\(topMatch, close\);\s*\n\s*setTerm\(""\);/);
  // Picks scatter through a long list, so what is about to be added is restated above it.
  assert.match(chipEditor, /\{multiple && picked\.length \? \(/);
  assert.match(chipEditor, /aria-label=\{`Unpick \$\{option\.label\}`\}/);
  assert.match(chipEditor, /onClick=\{\(\) => choose\(option\.id, close\)\}/);
  assert.match(styles, /\.filter-chip-editor__picks\s*\{/);
});

test("a batch add stays inside the server's selection limits", () => {
  const places = filterBar.match(/const addPlaces = [\s\S]*?\n  \};/)?.[0];
  assert.ok(places, "FilterBar should own one batch place transaction");
  assert.match(places, /\.slice\(0, MAX_CITY_FILTERS\)/);
  assert.match(places, /place\.kind === "city"/);
  assert.match(places, /place\.kind === "scope"/);
  assert.match(places, /new Set\(/);
  const topics = filterBar.match(/const addTopics = [\s\S]*?\n  \};/)?.[0];
  assert.ok(topics, "FilterBar should own one batch topic transaction");
  assert.match(topics, /\.slice\(0, MAX_TOPIC_FILTERS\)/);
});

test("source selection is a set the API repeats", () => {
  const body = filterBar.match(/const addSources = [\s\S]*?\n  \};/)?.[0];
  assert.ok(body, "FilterBar should own one batch source transaction");
  assert.match(body, /\.slice\(0, MAX_SOURCE_FILTERS\)/);
  assert.match(filterBar, /const MAX_SOURCE_FILTERS = 40/);
  assert.match(filterBar, /filters\.sourceKeys\.map\(\(sourceKey\) => \(/);
  assert.match(filterBar, /summary="Add source"/);
  const edit = filterBar.match(/const applySourceEdit = [\s\S]*?\n  \};/)?.[0];
  assert.ok(edit, "editing one source chip must not disturb the others");
  assert.match(edit, /key === current \? next : key/);
});

test("a date picked from the search box replaces the date filter", () => {
  // Picking a date answers "when", so it must not stack a second DATE chip.
  // "Add dates" stays the one deliberate way to ask for a second window.
  const apply = filterBar.match(/const applySuggestion = [\s\S]*?\n  \};/)?.[0];
  assert.ok(apply, "FilterBar should own one suggestion transaction");
  assert.match(
    apply,
    /for \(const atomic of atomicFilters\) \{\n\s*next = applyAtomicSuggestion\(next, atomic\);\n\s*\}/,
    "every suggested filter goes through applyAtomicSuggestion, dates included",
  );
  assert.doesNotMatch(apply, /atomic\.kind === "date"/, "no additive special case for dates");
  assert.doesNotMatch(apply, /MAX_DATE_RANGES/);
  // applyAtomicSuggestion is what makes that a replace: exactly one range out.
  const atomic = filterBar.match(/function applyAtomicSuggestion[\s\S]*?\n\}/)?.[0];
  assert.ok(atomic, "one atomic transaction should own the date rewrite");
  assert.match(atomic, /next\.dateRanges = \[\{/);
  assert.doesNotMatch(atomic, /\.\.\.next\.dateRanges/, "a suggested date replaces, never appends");

  // The additive path survives, and it is the button rather than the search box.
  assert.match(filterBar, /if \(target === "new"\)/);
  assert.match(filterBar, /nextRanges\.push\(\{/);
  assert.match(filterBar, /onApply=\{\(start, end\) => applyDateRange\("new", start, end\)\}/);
  assert.match(filterBar, /disabled=\{dateRanges\.length >= MAX_DATE_RANGES\}/);
});

test("a typed expression composes sources and replaces the whole price selection", () => {
  const chat = readFileSync(new URL("../lib/chat.ts", import.meta.url), "utf8");
  const expression = chat.match(
    /export function interpretCatalogFilterExpression[\s\S]*?\n\}/,
  )?.[0];
  assert.ok(expression, "one function should own the typed-expression transaction");
  // Source is multi-valued now, so a typed source composes like a place or topic.
  assert.match(
    expression,
    /if \(!filters\.sourceKeys\.includes\(parsedSource\)\) filters\.sourceKeys\.push\(parsedSource\)/,
  );
  assert.doesNotMatch(expression, /filters\.sourceKeys = parsed\.sourceKeys/);
  // Pushing requires a copy, or the caller's own array is mutated in place.
  assert.match(chat, /sourceKeys: \[\.\.\.baseFilters\.sourceKeys\]/);
  // Price is one selection across three fields; half-replacing it strands a
  // comparison that reappears the moment the category changes.
  assert.match(expression, /filters\.priceComparison = parsed\.priceComparison/);
  assert.match(expression, /filters\.priceMinDollars = parsed\.priceMinDollars/);
  assert.match(expression, /filters\.priceMaxDollars = parsed\.priceMaxDollars/);
  // A typed date still replaces rather than accumulating.
  assert.match(expression, /filters\.dateRanges = \[\]/);
});

test("arrowing the suggestions is something a reader can see and follow", () => {
  // The keys already worked; nothing showed which suggestion they had reached.
  // Hover can afford to be a whisper because the pointer says where it is -
  // arrowing has nothing else to point with, and #101011 over a #0a0a0b panel
  // read as no change at all.
  assert.match(
    styles,
    /\.filter-suggestions button\.is-active \{[\s\S]*?box-shadow:[\s\S]*?inset 3px 0 0 0 var\(--accent\);/,
  );
  // Hover keeps its own quieter treatment rather than sharing the selected one.
  assert.match(styles, /\.filter-suggestions button:hover \{\s*background: var\(--panel-raised\);\s*\}/);
  // The list scrolls at 300px, so the selection has to be followed there.
  assert.match(styles, /\.filter-suggestions \{[\s\S]*?max-height: 300px;/);
  assert.match(
    filterBar,
    /suggestionListRef\.current\s*\?\.querySelector<HTMLElement>\(`#smart-filter-suggestion-\$\{activeSuggestion\}`\)\s*\?\.scrollIntoView\(\{ block: "nearest" \}\)/,
  );
  assert.match(filterBar, /ref=\{suggestionListRef\}/);
});
