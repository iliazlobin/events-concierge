import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { emptyCatalogFilters, initialCatalogFilters } from "../lib/catalog-filters.ts";
import {
  describeCatalogSelection,
  suggestSavedFilterName,
  uniqueSavedFilterName,
} from "../lib/catalog-filter-name.ts";
import { getSavedFilterSuggestions } from "../lib/filter-suggestions.ts";

const filterBar = readFileSync(new URL("../components/filter-bar.tsx", import.meta.url), "utf8");
const picker = readFileSync(
  new URL("../components/saved-filter-picker.tsx", import.meta.url),
  "utf8",
);
const app = readFileSync(new URL("../components/concierge-app.tsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
const accountMenu = readFileSync(new URL("../components/account-menu.tsx", import.meta.url), "utf8");
const settingsShell = readFileSync(
  new URL("../components/settings/settings-shell.tsx", import.meta.url),
  "utf8",
);
const savedFiltersPanel = readFileSync(
  new URL("../components/settings/saved-filters-panel.tsx", import.meta.url),
  "utf8",
);
const filterName = readFileSync(
  new URL("../lib/catalog-filter-name.ts", import.meta.url),
  "utf8",
);

const NOW = new Date("2026-08-26T12:00:00-07:00");

test("a new session starts somewhere real and Reset still clears everything", () => {
  const initial = initialCatalogFilters();
  assert.deepEqual(initial.cities, ["sanfrancisco"]);
  assert.equal(initial.city, "sanfrancisco");
  assert.equal(initial.datePreset, "week");
  // Reset is a different intent from "start here", and must stay truly unfiltered.
  const empty = emptyCatalogFilters();
  assert.deepEqual(empty.cities, []);
  assert.equal(empty.datePreset, "all");
  assert.match(app, /const DEFAULT_FILTERS = initialCatalogFilters\(\)/);
  assert.match(filterBar, /onChange\(emptyCatalogFilters\(\), "push"\)/);
});

test("the offered name describes the selection rather than numbering it", () => {
  const base = initialCatalogFilters();
  assert.equal(suggestSavedFilterName(base, [], [], NOW), "San Francisco · this week");
  assert.equal(
    suggestSavedFilterName({ ...base, availability: "available" }, [], [], NOW),
    "San Francisco · registration open · this week",
  );

  const rich = {
    ...base,
    cities: ["sanfrancisco", "oakland"],
    topics: ["ai"],
    sourceKeys: ["luma-bay-area"],
    price: "any",
    priceComparison: "at-most",
    priceMaxDollars: "20",
  };
  const name = suggestSavedFilterName(
    rich,
    [{ source_key: "luma-bay-area", display_name: "Luma Bay Area", label: "", publisher: "", provider: "", seed_url: "", event_count: 0 }],
    [{ topic: "ai", label: "AI", event_count: 0 }],
    NOW,
  );
  assert.equal(name, "San Francisco & Oakland · AI · Luma Bay Area · under $20 · this week");

  // Many places collapse rather than running past the column.
  const many = { ...base, cities: ["sanfrancisco", "oakland", "berkeley"] };
  assert.match(suggestSavedFilterName(many, [], [], NOW), /^San Francisco \+2 · this week$/);
  assert.equal(suggestSavedFilterName(emptyCatalogFilters(), [], [], NOW), "All events");
  assert.ok(suggestSavedFilterName(rich, [], [], NOW).length <= 80);
  assert.equal(
    suggestSavedFilterName({ ...base, availability: "sold_out" }, [], [], NOW),
    "San Francisco · sold out · this week",
  );
});

test("a generated name does not collide with one the tenant already kept", () => {
  assert.equal(uniqueSavedFilterName("SF · this week", []), "SF · this week");
  assert.equal(uniqueSavedFilterName("SF · this week", ["SF · this week"]), "SF · this week (2)");
  // Names are compared the way a reader compares them.
  assert.equal(uniqueSavedFilterName("SF", ["sf", "SF (2)"]), "SF (3)");
});

test("saved selections are reachable by name from the search box", () => {
  const saved = [
    { saved_filter_id: "a", name: "Zulu tech nights" },
    { saved_filter_id: "b", name: "Alpha weeknights" },
  ];
  const matches = getSavedFilterSuggestions("zulu", saved);
  assert.equal(matches.length, 1);
  assert.equal(matches[0].kind, "saved");
  assert.equal(matches[0].value, "a");
  assert.equal(matches[0].description, "Saved filter");
  // A named thing the reader kept outranks a coincidental city match.
  assert.ok(matches[0].score > 100);
  assert.equal(getSavedFilterSuggestions("nothing like it", saved).length, 0);

  // Applying one replaces the whole strip instead of composing with what is active.
  assert.match(filterBar, /if \(suggestion\.kind === "saved"\) \{/);
  assert.match(filterBar, /onApplySavedFilter\(entry\)/);
  assert.match(filterBar, /if \(suggestion\.kind === "saved"\) return "Saved"/);
});

test("the only word a reader has for the category reaches the whole category", () => {
  const saved = [
    { saved_filter_id: "a", name: "San Francisco · free · Aug 24–Aug 30" },
    { saved_filter_id: "b", name: "Oakland · AI · this month" },
    { saved_filter_id: "c", name: "Weeknights I might actually go to" },
    { saved_filter_id: "d", name: "Bay Area · free" },
  ];
  // Every other kind answers its own name -- "place" opens the place composer. These had no such
  // word, so asking for them by the only name they have returned nothing.
  for (const term of ["filter", "filters", "saved", "saved filters", "My Filters"]) {
    const matches = getSavedFilterSuggestions(term, saved);
    assert.equal(matches.length, 4, `"${term}" should reach every kept selection`);
    // Equal scores keep the server's recency order.
    assert.deepEqual(matches.map((match) => match.value), ["a", "b", "c", "d"]);
  }
  // A term that names one of them still means that one, not the list.
  const byName = getSavedFilterSuggestions("oakland", saved);
  assert.equal(byName.length, 1);
  assert.equal(byName[0].value, "b");
  // And an unrelated term still means nothing.
  assert.equal(getSavedFilterSuggestions("nothing like it", saved).length, 0);
});

test("the strip says which kept selection it is holding", () => {
  // Ordering is not part of what a selection selects, so re-sorting must not drop the mark.
  assert.match(filterBar, /catalogFilterKey\(filters, \{ includeSort: false \}\)/);
  // Compared against what applying would actually produce, defaults and all.
  assert.match(filterBar, /const SAVED_FILTER_BASE = initialCatalogFilters\(\)/);
  assert.match(filterBar, /\{ \.\.\.SAVED_FILTER_BASE, \.\.\.entry\.filters \}/);
  assert.match(filterBar, /appliedId=\{appliedSavedFilterId\}/);
  assert.match(
    filterBar,
    /if \(suggestion\.kind === "saved"\) return suggestion\.value === appliedSavedFilterId/,
  );
  // The trigger is the only part on screen most of the time, so it carries the state too.
  assert.match(picker, /applied \? <BookmarkCheck aria-hidden="true" \/> : <Bookmark/);
  assert.match(picker, /aria-current=\{isApplied \? "true" : undefined\}/);
  // No "Applied" word on the row: the tick and the lit row already say it.
  assert.match(picker, /<small>\{usedLabel\(entry, now\)\}<\/small>/);
  assert.doesNotMatch(picker, /<small>\{isApplied/);
  // Saving a selection that is already kept would store the same one twice, so it is not offered.
  assert.match(picker, /if \(!name \|\| busy \|\| applied\) return;/);
  assert.match(picker, /Already saved as<\/span>\s*\n\s*<strong>\{applied\.name\}<\/strong>/);
  // The name field is gone in that state, so focus has a mounted fallback inside the popover.
  assert.match(picker, /const keepFocusInside = \(\) => \{/);
  assert.doesNotMatch(picker, /nameInputRef\.current\?\.focus\(\)/);
  // A mark that only appears under the pointer reports nothing, so this one is persistent.
  assert.match(
    styles,
    /\.saved-filter-picker__row\.is-applied > button:first-child > svg \{[^}]*opacity: 1;/,
  );
  // The name replaces the count on the trigger, so it is clamped rather than pushing the strip.
  assert.match(styles, /\.saved-filter-picker__trigger-label \{[^}]*text-overflow: ellipsis;/);
});

test("the picker saves, reapplies, and forgets a selection", () => {
  // A whole selection is not another single-dimension filter, so it leads the strip and is drawn
  // as its own class of control rather than as one more "Add …" chip.
  assert.match(
    filterBar,
    /aria-label="Active filters">\s*(?:\{\/\*[\s\S]*?\*\/\}\s*)?\{signedIn \? \(\s*<SavedFilterPicker/,
  );
  assert.match(styles, /\.saved-filter-picker \{[^}]*border-right:/);
  assert.match(styles, /\.saved-filter-picker__popover \{[^}]*left: 0;/);
  assert.match(filterBar, /suggestedName=\{uniqueSavedFilterName\(/);
  assert.match(picker, /role="dialog"/);
  assert.match(picker, /aria-label="Saved filters"/);
  // Recency is the default order and the toggle is the escape hatch.
  assert.match(picker, /useState<SavedFilterSort>\("recent"\)/);
  assert.match(picker, /sort === "name"\s*\?\s*matched\.sort\(/);
  assert.match(picker, /current === "recent" \? "name" : "recent"/);
  assert.match(picker, /entry\.name\.toLocaleLowerCase\(\)\.includes\(needle\)/);
  assert.match(picker, /aria-label=\{`Delete \$\{entry\.name\}`\}/);
  assert.match(styles, /\.saved-filter-picker__popover\s*\{/);
});

test("deleting a kept selection is visible before the pointer arrives, and asks once", () => {
  // The tick is the hover echo; scoping the fade to it is what keeps the delete control on screen.
  assert.match(styles, /\.saved-filter-picker__row > button:first-child > svg \{[^}]*opacity: 0;/);
  assert.doesNotMatch(styles, /\.saved-filter-picker__row svg \{[^}]*opacity: 0;/);
  assert.match(styles, /\.saved-filter-picker__row:hover \.saved-filter-picker__delete,/);
  // The list reuses `.filter-chip-editor__options button`, a three-column grid whose leading glyph
  // is transparent until the row is active. Every rule for the delete control has to outrank that
  // on specificity -- source order cannot save it -- or the button renders and shows nothing.
  assert.match(
    styles,
    /\.saved-filter-picker__row \.saved-filter-picker__delete > svg \{[^}]*opacity: 1;/,
  );
  assert.doesNotMatch(styles, /\n\.saved-filter-picker__delete[ ,:{]/);
  // Deleting is immediate: no question, by owner decision. The row it unmounts was holding
  // focus, and the popover closes on any blur that leaves it, so focus is handed back inside.
  assert.match(picker, /onDelete\(entry\);\s*\n(?:.*\n)*?\s*keepFocusInside\(\);/);
  assert.doesNotMatch(picker, /pendingDelete/);
  assert.doesNotMatch(picker, /confirm/);
  assert.doesNotMatch(styles, /is-confirming/);
  // A row holding two controls is a group, not a listbox of values.
  assert.doesNotMatch(picker, /role="listbox"/);
  assert.doesNotMatch(picker, /role="option"/);
});

test("a starting-point tile draws its kind rather than spelling it", () => {
  // The tile's kind column is 22px wide, which no word fits -- a word there lands on the label.
  assert.match(styles, /\.filter-suggestions\.is-starters button \{[^}]*grid-template-columns: 22px/);
  assert.match(styles, /\.filter-suggestion__mark \{[^}]*width: 22px;/);
  assert.match(filterBar, /function suggestionKindIcon\(suggestion: SmartFilterSuggestion\)/);
  assert.match(filterBar, /if \(suggestion\.kind === "saved"\) return <Bookmark aria-hidden="true" \/>/);
  assert.match(filterBar, /const startersLayout = !query\.trim\(\)/);
  assert.match(filterBar, /\) : startersLayout \? \(\s*<span className="filter-suggestion__mark">/);
  // The word is still what a screen reader hears.
  assert.match(filterBar, /<span className="sr-only">\{suggestionKindLabel\(suggestion\)\}<\/span>/);
  // A kept selection reads a shade warmer than the ones we propose, and hover still wins.
  const savedTile = styles.indexOf('.filter-suggestions.is-starters button[data-kind="saved"]');
  const hover = styles.indexOf(".filter-suggestions.is-starters button:hover");
  assert.ok(savedTile > 0 && hover > savedTile, "the saved tile rule must precede the hover rule");
});

test("the account menu carries a page for managing what was saved", () => {
  assert.match(accountMenu, /href: "\/settings\/saved-filters", label: "Saved filters"/);
  assert.match(settingsShell, /\{ href: "\/settings\/saved-filters", label: "Saved filters" \}/);
  // Renaming and deleting do not belong beside a control whose job is to apply, so they live here.
  assert.match(savedFiltersPanel, /replaceSavedFilter\(\s*entry\.saved_filter_id,\s*name,/);
  assert.match(savedFiltersPanel, /deleteSavedFilter\(entry\.saved_filter_id, tenantId\)/);
  assert.match(savedFiltersPanel, /pendingDelete === entry\.saved_filter_id/);
  // An older payload may predate a filter field, so it is layered over the defaults before use.
  assert.match(savedFiltersPanel, /\{ \.\.\.DEFAULT_FILTERS, \.\.\.saved\.filters \}/);
  // The description of a selection has one source, so a rename cannot make it lie.
  assert.match(savedFiltersPanel, /describeCatalogSelection\(/);
  assert.match(
    filterName,
    /export function suggestSavedFilterName\([\s\S]*?describeCatalogSelection\(filters, providers, topics, now\)\.join\(" · "\)/,
  );
});

test("the selection a saved filter holds survives a rename", () => {
  const base = initialCatalogFilters();
  assert.deepEqual(describeCatalogSelection(base, [], [], NOW), ["San Francisco", "this week"]);
  assert.deepEqual(describeCatalogSelection(emptyCatalogFilters(), [], [], NOW), []);
  // The offered name is exactly those parts joined, so the two can never drift apart.
  assert.equal(
    describeCatalogSelection(base, [], [], NOW).join(" · "),
    suggestSavedFilterName(base, [], [], NOW),
  );
});

test("applying a saved selection is a history transaction and updates recency", () => {
  const body = app.match(/const handleApplySavedFilter = [\s\S]*?\n  \}, \[[^\]]*\]\);/)?.[0];
  assert.ok(body, "one handler should own applying a saved selection");
  assert.match(body, /pushConsumerSnapshot\(createConsumerHistorySnapshot\(/);
  // An older saved payload may predate a filter field, so it is layered over the defaults.
  assert.match(body, /\{ \.\.\.DEFAULT_FILTERS, \.\.\.saved\.filters \}/);
  assert.match(body, /recordSavedFilterUse\(saved\.saved_filter_id, tenantId\)/);
  // A failed save has to say so rather than silently doing nothing.
  assert.match(app, /setSavedFiltersError\(readableError\(error\)\)/);
});
