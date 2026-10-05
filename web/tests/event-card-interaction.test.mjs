import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const card = readFileSync(
  new URL("../components/event-card.tsx", import.meta.url),
  "utf8",
);
const list = readFileSync(
  new URL("../components/event-list.tsx", import.meta.url),
  "utf8",
);
const app = readFileSync(
  new URL("../components/concierge-app.tsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);

test("event details retain a native disclosure control", () => {
  assert.match(
    card,
    /className="event-card__summary-trigger"\s+type="button"\s+aria-expanded=\{expanded\}\s+aria-controls=\{regionId\}/s,
  );
  assert.match(card, /className="event-card__disclosure-cue"/);
  assert.match(styles, /\.event-card__summary-trigger:focus-visible/);
  assert.doesNotMatch(card, /event-card__reveal/);
  assert.doesNotMatch(styles, /event-card__reveal/);
});

test("calendar and map links remain independent controls above the summary target", () => {
  const triggerIndex = card.indexOf('className="event-card__summary-trigger"');
  const linkIndex = card.indexOf('className="event-card__meta-link"');
  assert.ok(triggerIndex >= 0 && triggerIndex < linkIndex);
  assert.match(card, /target="_blank"\s+rel="noopener noreferrer"/s);
  assert.match(
    styles,
    /\.event-card__meta-link\s*\{[^}]*position: relative;[^}]*z-index: 2;/s,
  );
  assert.match(styles, /\.event-card__meta-link:focus-visible/);
  assert.doesNotMatch(card, /<article[^>]*onClick/);
});

test("event source labels apply the exact provider filter without opening the card", () => {
  assert.match(card, /function providerFilter\(event: EventItem\)/);
  assert.match(card, /event\.source_keys\?\.find/);
  assert.match(card, /className="event-card__source-filter"/);
  assert.match(card, /aria-label=\{`Filter events by source \$\{provider\.label\}`\}/);
  assert.match(
    card,
    /interaction\.preventDefault\(\);[\s\S]*interaction\.stopPropagation\(\);[\s\S]*onSourceSelect\(provider\.sourceKey\);/,
  );
  assert.match(list, /onSourceSelect=\{onSourceSelect\}/);
  assert.match(
    styles,
    /\.event-card__source-filter\s*\{[^}]*position: relative;[^}]*z-index: 2;/s,
  );
  assert.match(styles, /\.event-card__source-filter:focus-visible/);
  assert.match(
    app,
    /const handleSourceSelect = useCallback[\s\S]*createConsumerHistorySnapshot\("events"[\s\S]*sourceKeys: \[sourceKey\],[\s\S]*datePreset: "source",[\s\S]*cities: \[\],[\s\S]*pushConsumerSnapshot/,
  );
});

test("expanded details stay labelled, inert when closed, and one-open-at-a-time", () => {
  assert.match(
    card,
    /role="region"\s+aria-labelledby=\{titleId\}\s+aria-hidden=\{!expanded\}\s+inert=\{!expanded\}/s,
  );
  assert.match(
    list,
    /expandedId === event\.canonical_event_id\s*\?\s*null\s*:\s*event\.canonical_event_id/s,
  );
});

test("structured facts are compact, searchable, and isolate native facet controls", () => {
  const entityLinksStart = card.indexOf("function EntityLinks");
  const entityLinksEnd = card.indexOf("export function EventCard", entityLinksStart);
  const entityLinks = card.slice(entityLinksStart, entityLinksEnd);
  const entityButtonStart = entityLinks.indexOf("<button");
  const entityButtonEnd = entityLinks.indexOf("</button>", entityButtonStart);
  const entityButton = entityLinks.slice(entityButtonStart, entityButtonEnd);

  assert.match(card, /className="event-card__decision-strip"/);
  assert.match(card, /className="event-card__entities"/);
  assert.doesNotMatch(entityButton, /onPointerDown/);
  assert.match(card, /interaction\.preventDefault\(\);[\s\S]*interaction\.stopPropagation\(\);[\s\S]*onEntitySelect\(\{ canonicalEventId, role, name \}\);/);
  assert.match(card, /aria-label=\{`Explore \$\{roleLabel\.toLowerCase\(\)\} \$\{name\}`\}/);
  assert.match(card, /safeEntityProfile\(profile\.profile_url, defaultKind\)/);
  assert.doesNotMatch(card, /safeEntityProfile\(profile\.profile_url, profile\.kind\)/);
  assert.match(card, /aria-label=\{profileLabel\}/);
  assert.match(card, /target="_blank"\s+rel="noopener noreferrer"/s);
  assert.doesNotMatch(card, /Search LinkedIn for/);
  assert.doesNotMatch(card, /linkedinSearchUrl/);
  assert.match(card, />Organizations</);
  assert.doesNotMatch(card, /Partners \/ vendors/);
  assert.doesNotMatch(card, />Participants</);
  assert.match(
    app,
    /document\.activeElement\.blur\(\);[\s\S]*pushConsumerSnapshot\(nextSnapshot\)/,
  );
  assert.match(app, /window\.requestAnimationFrame\(\(\) => \{[\s\S]*window\.scrollTo/);
});
