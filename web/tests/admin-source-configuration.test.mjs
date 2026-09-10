import assert from "node:assert/strict";
import test from "node:test";

import { safeHttpUrl } from "../lib/admin-presentation.ts";
import { collectionHorizonDays, collectionHorizonLabel } from "../lib/admin-source-configuration.ts";

test("source links preserve the recorded endpoint path and query", () => {
  const endpoint = "https://events.example.test/api/events?calendar=arts&after=2026-09-08";
  assert.equal(safeHttpUrl(endpoint), endpoint);
  assert.equal(safeHttpUrl("https://events.example.test"), "https://events.example.test/");
  assert.equal(safeHttpUrl("http://events.example.test/calendar"), "http://events.example.test/calendar");
});

test("event-window bounds accept recorded whole days from one through ninety", () => {
  for (const days of [1, 30, 60, 90]) assert.equal(collectionHorizonDays(days), days);
  assert.equal(collectionHorizonLabel(1), "Upcoming 1 day");
  assert.equal(collectionHorizonLabel(90), "Upcoming 90 days");
});

test("legacy and invalid windows remain unknown instead of receiving an inferred default", () => {
  for (const value of [undefined, null, 0, -1, 91, 1.5, Infinity, NaN, "90", ""]) {
    assert.equal(collectionHorizonDays(value), null);
    assert.equal(collectionHorizonLabel(value), "Not recorded");
  }
});

test("untrusted source link values cannot become executable or relative links", () => {
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "//example.test/events",
    "/events",
    "example.test",
    "",
  ]) {
    assert.equal(safeHttpUrl(value), null, value);
  }
});
