import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const mapView = readFileSync(
  new URL("../components/map-view.tsx", import.meta.url),
  "utf8",
);
const previewRail = readFileSync(
  new URL("../components/map-preview-rail.tsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);
const dayTrack = readFileSync(
  new URL("../components/map-day-track.tsx", import.meta.url),
  "utf8",
);

test("blank map clicks clear the active card without relying on map load timing", () => {
  assert.match(mapView, /className="map-canvas"\s+onClick=/);
  assert.match(mapView, /target\.closest\("\.map-marker, \.maplibregl-ctrl"\)/);
  assert.match(mapView, /setSelectedId\(null\)/);
});

test("the preview rail tracks map bounds and focuses without rebuilding markers", () => {
  assert.match(mapView, /map\.on\("moveend", syncVisibleEvents\)/);
  assert.match(mapView, /eventsInMapBounds\(mapped, map\.getBounds\(\)\)/);
  assert.match(mapView, /map\.easeTo\(\{ \.\.\.view, duration: 420 \}\)/);
  assert.match(
    mapView,
    /\}, \[cities, locationScopes, mapReady, mapped, prefersReducedMotion, selectEvent\]\);/,
  );
  assert.match(mapView, /marker\.getElement\(\)\.classList\.toggle/);
});

test("preview cards expose selection, optional imagery, and a direct event link", () => {
  assert.match(previewRail, /aria-label="Events visible in map"/);
  assert.match(previewRail, /aria-pressed=\{selected\}/);
  assert.match(previewRail, /imageUrl \? \(/);
  assert.match(previewRail, />\s*View event\s*/);
  assert.match(previewRail, /target="_blank"/);
  assert.match(previewRail, /className="map-preview__source"/);
  assert.match(previewRail, /event\.calendar_labels\?\.find/);
});

test("the right rail scrolls vertically and moves below the map on narrow screens", () => {
  assert.match(
    styles,
    /\.map-preview-rail__list\s*\{[^}]*grid-auto-rows: max-content;[^}]*overflow-y: auto;/s,
  );
  assert.match(
    styles,
    /@media \(max-width: 760px\)[\s\S]*?\.map-preview-rail\s*\{[^}]*position: relative;/,
  );
  assert.match(
    styles,
    /\.map-selection\s*\{[^}]*left: 24px;[^}]*width: min\(620px,/s,
  );
});

test("map-specific chrome stays compact while the canvas owns the viewport", () => {
  assert.doesNotMatch(mapView, /EXPLORE BY PLACE/);
  assert.match(mapView, /<header className="map-heading">/);
  assert.match(
    styles,
    /\.map-heading h1\s*\{[^}]*font-size: clamp\(22px, 2vw, 28px\);/s,
  );
  assert.match(
    styles,
    /\.map-frame\s*\{[^}]*height: clamp\(\s*560px,\s*calc\(100dvh - var\(--header-height\) - 166px\),\s*900px\s*\);/s,
  );
  assert.match(
    styles,
    /--map-preview-rail-width: min\(270px, 22vw\);/,
  );
  assert.match(
    styles,
    /@media \(max-width: 760px\)[\s\S]*?\.map-frame\s*\{[^}]*height: clamp\(\s*390px,\s*calc\(100dvh - var\(--header-height\) - 230px\),\s*620px\s*\);/,
  );
});

test("the multi-day UI stays invisible until it has something to say", () => {
  assert.match(mapView, /const multiDay = dayModel\.cells\.length > 1;/);
  assert.match(mapView, /\{multiDay \? \(\s*<MapDayTrack/);
  assert.match(mapView, /\{multiDay \? null : \(\s*<div className="map-watermark">/);
  // The rail and the track mount on the same test, so they can never disagree.
  assert.match(previewRail, /const grouped = model\.cells\.length > 1;/);
});

test("day emphasis never rebuilds the markers or refits the camera", () => {
  // Creation-time tier is read from a ref, exactly as the selected id already is,
  // so the build effect's dependency array (asserted verbatim above) never grows.
  assert.match(mapView, /const emphasis = emphasisDayRef\.current;/);
  assert.match(mapView, /element\.dataset\.day = eventDayKey\(event\) \?\? "";/);
  // Runtime tier rides the existing class-toggle effect instead of a second one.
  assert.match(mapView, /\}, \[emphasisDay, selectedId\]\);/);
  // Promoting a day on select must not add a dependency to selectEvent, whose array
  // feeds the marker-rebuild effect.
  assert.match(
    mapView,
    /setActiveDay\(\(current\) => \(current \? eventDayKey\(event\) \?\? current : current\)\);/,
  );
  assert.match(mapView, /\}, \[prefersReducedMotion\]\);/);
  // Without this guard every `Load more` yanks the camera back to whole-result bounds
  // from wherever the reader had panned. Every other change still refits.
  assert.match(mapView, /const appended = fitted\.length > 0/);
  assert.match(mapView, /if \(appended\) return;/);
  assert.match(mapView, /top: 70 \+ trackInsetRef\.current,/);
});

test("marker visuals live on the inner dot because maplibre owns the root", () => {
  // maplibre-gl 6.0.0 writes inline transform (maplibre-gl-dev.mjs:26156) and inline
  // opacity (:26533) onto the marker root on every render, and maplibre-gl.css --
  // imported after globals.css -- overrides its transition. Anything put on
  // .map-marker itself passes review and then silently does nothing in the browser.
  assert.match(mapView, /dot\.className = "map-marker__dot";/);
  assert.match(styles, /\.map-marker__dot\s*\{[^}]*transform: scale\(var\(--dot-scale\)\);/s);
  assert.doesNotMatch(styles, /\.map-marker\s*\{[^}]*\bopacity:/s);
  assert.doesNotMatch(styles, /\.map-marker\s*\{[^}]*\btransform:/s);
  assert.doesNotMatch(styles, /\.map-marker\.is-muted\s*\{[^}]*\bopacity:/s);
});

test("source order is what keeps a dimmed pin reachable", () => {
  // All three selectors weigh the same, so only their order guarantees that hover
  // and selection out-rank the dim tier.
  const muted = styles.indexOf(".map-marker.is-muted");
  const hover = styles.indexOf(".map-marker:hover");
  const selected = styles.indexOf(".map-marker.is-selected");
  assert.ok(muted > -1, "dim tier is missing");
  assert.ok(muted < hover, "hover must be authored after .is-muted");
  assert.ok(hover < selected, "selection must be authored after hover");
});

test("dimming recedes without removing, at values a drive-by tweak cannot move", () => {
  assert.match(styles, /\.map-marker\.is-muted\s*\{[^}]*--dot-scale: 0\.62;/s);
  assert.match(styles, /\.map-marker\.is-muted\s*\{[^}]*--dot-fill: rgba\(245, 245, 240, 0\.3\);/s);
  assert.match(styles, /\.map-preview\.is-muted\s*\{[^}]*opacity: 0\.44;/s);
  assert.match(styles, /\.map-preview\.is-muted \.map-preview__image\s*\{[^}]*filter: grayscale\(1\)/s);
  // A dim card is still a live card.
  assert.match(styles, /\.map-preview\.is-muted:hover,\s*\.map-preview\.is-muted:focus-within\s*\{\s*opacity: 1;/);
  // Opacity is on the card, not the section, so the selected card can stay bright
  // inside a dimmed day -- a child cannot out-opacity its parent.
  assert.match(previewRail, /muted=\{muted && selectedId !== event\.canonical_event_id\}/);
});

test("the track is a toolbar of monochrome channels, never a colour ramp", () => {
  assert.match(dayTrack, /role="toolbar"/);
  assert.match(dayTrack, /aria-orientation="horizontal"/);
  assert.match(dayTrack, /aria-pressed=\{activeDay === null\}/);
  assert.match(dayTrack, /"--day-weight":/);
  assert.match(dayTrack, /"--coverage":/);
  assert.doesNotMatch(dayTrack, /hsl\(/);
  assert.doesNotMatch(styles, /\.map-day-cell[\s\S]{0,600}?hsl\(/);
});

test("three resets and two ways to hop, all one action", () => {
  // The reset sits in its own grid column, outside the only scrolling area, so it
  // is in the same place at day 1 and at day 40.
  assert.match(
    styles,
    /\.map-day-track\s*\{[^}]*grid-template-columns: auto minmax\(0, 1fr\) auto;/s,
  );
  assert.match(styles, /\.map-day-track__scroll\s*\{[^}]*overflow-y: auto;/s);
  assert.match(dayTrack, /onClick=\{\(\) => onActivate\(null\)\}/);
  assert.match(dayTrack, /onClick=\{\(\) => onActivate\(active \? null : cell\.key\)\}/);
  assert.match(previewRail, /className="map-preview-rail__filter"/);
  assert.match(previewRail, /onClick=\{\(\) => onDayActivate\(active \? null : group\.key\)\}/);
  assert.match(mapView, /activateDay\(stepDay\(keys, activeDayRef\.current, 1\)\);/);
  assert.match(mapView, /keyEvent\.key === "\]" \|\| keyEvent\.code === "BracketRight"/);
  assert.match(mapView, /keyEvent\.key === "Escape"/);
});

test("the rail groups by day without losing a day it is filtering to", () => {
  assert.match(previewRail, /className="map-preview-day__heading"/);
  assert.match(previewRail, /role="group"/);
  assert.match(previewRail, /className="map-preview-day__stub"/);
  assert.match(previewRail, /\[\.\.\.groups, \{ key: activeDay, events: \[\] \}\]/);
  assert.match(styles, /\.map-preview-day__heading\s*\{[^}]*position: sticky;/s);
});

test("the floating track never meets a full-height event card", () => {
  assert.match(
    styles,
    /\.map-stage\[data-day-track="on"\] \.map-selection\s*\{[^}]*max-height: calc\(100% - 48px - var\(--map-day-track-height/s,
  );
  assert.match(mapView, /stage\.style\.setProperty\("--map-day-track-height"/);
  // A 390px frame cannot afford a floating bar, and the rail width is 100% there,
  // so the desktop right offset would put the track off-canvas.
  assert.match(
    styles,
    /@media \(max-width: 760px\)[\s\S]*?\.map-day-track\s*\{[^}]*position: static;/,
  );
});
