import assert from "node:assert/strict";
import test from "node:test";
import { sourceEventSummary, sourceTitle, sourceWebsite } from "../lib/source-view.ts";

const source = {
  source_key: "example-calendar", display_name: "Example Calendar", label: "Example",
  publisher: "Example Organization", provider: "luma", seed_url: "https://lu.ma/example", event_count: 50,
};
const events = [
  { canonical_event_id: "1", start_at: "2026-10-16T12:00:00Z", city: "sanfrancisco", topics: ["ai", "ai", "founders"], sources: [{ source_key: source.source_key, label: "Example Calendar" }] },
  { canonical_event_id: "2", start_at: "2026-10-18T12:00:00Z", city: "San Francisco", topics: ["ai"], sources: [] },
];

test("source facts reflect loaded unique events, not the source's in-range count", () => {
  const result = sourceEventSummary([...events, events[0]]);
  assert.equal(result.loadedCount, 2);
  assert.deepEqual(result.cities, ["San Francisco"]);
  assert.deepEqual(result.topics, [{ key: "ai", label: "AI", count: 2 }, { key: "founders", label: "Founders", count: 1 }]);
  assert.match(result.dateLabel, /Oct/);
  assert.match(result.dateLabel, /16/);
  assert.match(result.dateLabel, /18/);
  assert.notEqual(result.loadedCount, source.event_count);
});

test("unavailable event facts stay absent", () => {
  assert.deepEqual(sourceEventSummary([]), { loadedCount: 0, dateLabel: null, cities: [], topics: [] });
  assert.deepEqual(sourceEventSummary([{ canonical_event_id: "3", start_at: "invalid", city: null, topics: [] }]), {
    loadedCount: 1, dateLabel: null, cities: [], topics: [],
  });
});

test("source title uses matching public source identity without inventing an organizer", () => {
  assert.equal(sourceTitle(source, source.source_key, events), "Example Calendar");
  assert.equal(sourceTitle(null, source.source_key, events), "Example Calendar");
  assert.equal(sourceTitle(null, "other-source", events), "Event source");
});

test("source website is a safe external web link", () => {
  assert.equal(sourceWebsite(source), "https://lu.ma/example");
  assert.equal(sourceWebsite(null), null);
  for (const seed_url of ["javascript:alert(1)", "data:text/html,x", "https://user:password@example.com", "https://example.com/\n", "https://example.com/\\evil", "", "bad"]) {
    assert.equal(sourceWebsite({ ...source, seed_url }), null);
  }
});
