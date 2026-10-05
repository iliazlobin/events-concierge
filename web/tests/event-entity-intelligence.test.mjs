import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// The entity detail surface moved out of `entities-view.tsx` and into the graph inspector, which is
// where the reader now meets it. These assertions follow the behaviour rather than the filename:
// the same public data must still be presented, and it must still be refreshable from the exact
// URLs a source attached.
const inspector = readFileSync(
  new URL("../components/entity-inspector.tsx", import.meta.url),
  "utf8",
);
const textView = readFileSync(
  new URL("../components/entity-graph-text.tsx", import.meta.url),
  "utf8",
);
const api = readFileSync(new URL("../lib/api.ts", import.meta.url), "utf8");

test("entity detail presents structured public data instead of pipeline evidence", () => {
  assert.match(inspector, /Public data/);
  assert.match(inspector, /Profiles/);
  assert.match(textView, /Upcoming/);
  assert.match(inspector, /external_facts/);
  assert.match(inspector, /external_sources/);
  assert.doesNotMatch(inspector, /EVENT EVIDENCE|ENTITY RESEARCH|Evidence first/);
  assert.doesNotMatch(inspector, /Auto-merge|research ready/);
});

test("entity detail can refresh its exact public sources", () => {
  assert.match(api, /catalog\/entities\/\$\{encodeURIComponent\(entityId\)\}\/refresh/);
  assert.match(inspector, /refreshCatalogEntity/);
  assert.match(inspector, /detail\?\.refresh_due/);
  assert.match(inspector, /Refreshing/);
});

test("refresh is offered only where an exact profile URL exists to re-read", () => {
  // A source-scoped record has no direct URL, so offering it a refresh would imply we could go
  // looking for one — which is exactly the name-derived lookup the render contract forbids.
  assert.match(inspector, /identity_status === "profile_verified"/);
});

test("nothing in the entity surface turns a name into a profile lookup", () => {
  for (const source of [inspector, textView]) {
    assert.doesNotMatch(source, /linkedin\.com\/search/i);
    assert.doesNotMatch(source, /google\.com\/search/i);
  }
});
