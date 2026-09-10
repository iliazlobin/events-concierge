import assert from "node:assert/strict";
import test from "node:test";
import { registrationTimestamp, sourceHistoryDays, sourceRegistrationPlot } from "../lib/admin-source-registration-history.ts";

const history = {
  generated_at: "2026-09-08T16:00:00Z", window_start: "2026-09-06T16:00:00Z",
  baseline_sources: 37, total_sources: 42, added_sources: 5,
  items: [
    { bucket_start: "2026-09-06T16:00:00Z", bucket_end: "2026-09-07T16:00:00Z", registered_sources: 37, added_sources: 0 },
    { bucket_start: "2026-09-07T16:00:00Z", bucket_end: "2026-09-08T16:00:00Z", registered_sources: 42, added_sources: 5 },
  ],
};

test("registration chart carries the prior count and preserves quiet daily intervals", () => {
  const plot = sourceRegistrationPlot(history);
  assert.deepEqual(plot.points.map(({x, count, added}) => ({x, count, added})), [
    {x: 0, count: 37, added: 0}, {x: 500, count: 37, added: 0}, {x: 1000, count: 42, added: 5},
  ]);
  assert.equal(plot.maximum, 42);
  assert.equal(plot.points[0].y, plot.points[1].y);
  assert.equal(plot.points[2].y, 10);
  assert.match(plot.path, /H 500 V .* H 1000 V 10$/);
  assert.doesNotMatch(plot.path, /NaN|Infinity/);
});

test("empty registries have an honest flat zero series with no artificial additions", () => {
  const plot = sourceRegistrationPlot({...history, baseline_sources: 0, total_sources: 0, added_sources: 0,
    items: history.items.map(item => ({...item, registered_sources: 0, added_sources: 0})),
  });
  assert.equal(plot.maximum, 1);
  assert.ok(plot.points.every(point => point.y === 190 && point.count === 0));
  assert.doesNotMatch(plot.path, /NaN|Infinity/);
});

test("registration windows are bounded and readouts are unambiguous UTC", () => {
  for (const value of [null, "", "all", "0", "-7", "365"]) assert.equal(sourceHistoryDays(value), 90);
  assert.equal(sourceHistoryDays("7"), 7);
  assert.equal(sourceHistoryDays("30"), 30);
  assert.equal(sourceHistoryDays("90"), 90);
  assert.equal(registrationTimestamp("2026-09-08T09:00:00-07:00"), "2026-09-08 16:00 UTC");
});
