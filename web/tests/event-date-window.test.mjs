import assert from "node:assert/strict";
import test from "node:test";

import {
  dateWindow,
  normalizeDateRangeFilters,
  parseDateRangeParameter,
  semanticDateRangeKeys,
  serializeDateRangeFilter,
  serializeDateRangeFilterForApi,
} from "../lib/date.ts";

const NOW = new Date("2026-07-31T15:42:13-07:00");

test("upcoming presets start at the current moment, not local midnight", () => {
  for (const preset of ["today", "week", "month"]) {
    const window = dateWindow(preset, "", "", NOW);
    assert.equal(window.start.toISOString(), NOW.toISOString());
  }
});

test("This week on the reported Wednesday starts October 7 and ends after Sunday", () => {
  const now = new Date(2026, 9, 7, 9, 30);
  const window = dateWindow("week", "", "", now);
  assert.equal(window.start.getTime(), now.getTime());
  assert.deepEqual([
    window.start.getDate(), window.end.getDate(), window.end.getDay(), window.end.getHours(),
  ], [7, 12, 1, 0]);
});

test("local-day serialization retains both daylight-saving transitions", () => {
  const previousZone = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    for (const [day, expectedHours] of [["2026-03-08", 23], ["2026-11-01", 25]]) {
      const [start, end] = serializeDateRangeFilterForApi({ id: day, start: day, end: day })
        .split("..").map(value => new Date(value));
      assert.equal(start.getHours(), 0);
      assert.equal(end.getHours(), 0);
      assert.equal((end - start) / 3_600_000, expectedHours);
    }
  } finally {
    if (previousZone === undefined) delete process.env.TZ;
    else process.env.TZ = previousZone;
  }
});

test("an active weekend starts now while a future weekend starts on Saturday", () => {
  const friday = dateWindow("weekend", "", "", NOW);
  assert.ok(friday.start > NOW);

  const saturdayNow = new Date("2026-08-01T09:30:00-07:00");
  const saturday = dateWindow("weekend", "", "", saturdayNow);
  assert.equal(saturday.start.toISOString(), saturdayNow.toISOString());
});

test("semantic week and weekend choices retain full calendar ranges at a Sunday boundary", () => {
  const sundayBoundary = new Date(2026, 7, 2, 12, 0, 0);

  const week = semanticDateRangeKeys("week", "", "", sundayBoundary);
  assert.deepEqual(week, {
    start: "2026-07-27",
    end: "2026-08-02",
  });
  assert.notEqual(week.start, week.end);

  const weekend = semanticDateRangeKeys("weekend", "", "", sundayBoundary);
  assert.deepEqual(weekend, {
    start: "2026-08-01",
    end: "2026-08-02",
  });
  assert.notEqual(weekend.start, weekend.end);
});

test("explicit custom windows retain their full selected calendar days", () => {
  const window = dateWindow("custom", "2026-07-01", "2026-07-31", NOW);
  assert.equal(window.start.getHours(), 0);
  assert.equal(window.end.getDate(), 1);
  assert.equal(window.end.getMonth(), 7);
});

test("a selected source receives a bounded retained-history window", () => {
  const window = dateWindow("source", "", "", NOW);
  const days = (window.end.valueOf() - window.start.valueOf()) / 86_400_000;
  assert.ok(days > 365);
  assert.ok(days <= 7_305);
  assert.equal(window.label, "All retained source events");
});

test("additive date ranges normalize, deduplicate, and preserve stable identities", () => {
  const ranges = normalizeDateRangeFilters([
    { id: "weekend-a", start: "2026-08-08", end: "2026-08-09", label: "Next weekend" },
    { id: "duplicate", start: "2026-08-08", end: "2026-08-09" },
    { id: "reversed", start: "2026-08-16", end: "2026-08-15" },
    { id: "invalid", start: "2026-02-30", end: "2026-03-01" },
  ]);

  assert.deepEqual(ranges, [
    { id: "weekend-a", start: "2026-08-08", end: "2026-08-09", label: "Next weekend" },
    { id: "reversed", start: "2026-08-15", end: "2026-08-16" },
  ]);
  assert.equal(serializeDateRangeFilter(ranges[0]), "2026-08-08..2026-08-09");
  assert.deepEqual(parseDateRangeParameter("2026-08-15..2026-08-16"), {
    id: "2026-08-15..2026-08-16",
    start: "2026-08-15",
    end: "2026-08-16",
  });
  assert.equal(parseDateRangeParameter("2026-08-15/2026-08-16"), null);
});

test("catalog transport uses local midnight rather than UTC calendar boundaries", () => {
  const serialized = serializeDateRangeFilterForApi({
    id: "week",
    start: "2026-08-03",
    end: "2026-08-09",
  });
  const [startValue, endValue] = serialized.split("..");
  const start = new Date(startValue);
  const end = new Date(endValue);

  assert.equal(start.getFullYear(), 2026);
  assert.equal(start.getMonth(), 7);
  assert.equal(start.getDate(), 3);
  assert.equal(start.getHours(), 0);
  assert.equal(end.getFullYear(), 2026);
  assert.equal(end.getMonth(), 7);
  assert.equal(end.getDate(), 10);
  assert.equal(end.getHours(), 0);
  assert.equal(serializeDateRangeFilter({
    id: "week",
    start: "2026-08-03",
    end: "2026-08-09",
  }), "2026-08-03..2026-08-09");
});

test("a work week runs Monday to Friday, and rolls forward from a weekend", () => {
  // Thursday: the working week already under way.
  const thursday = new Date(2026, 7, 27, 12, 0, 0);
  assert.deepEqual(semanticDateRangeKeys("workweek", "", "", thursday), {
    start: "2026-08-24",
    end: "2026-08-28",
  });
  // Asked on a Saturday or a Sunday, the working week worth asking about is the one that has
  // not started yet -- the one just ended would return nothing.
  for (const weekendDay of [new Date(2026, 7, 29, 12, 0, 0), new Date(2026, 7, 30, 12, 0, 0)]) {
    assert.deepEqual(semanticDateRangeKeys("workweek", "", "", weekendDay), {
      start: "2026-08-31",
      end: "2026-09-04",
    });
  }
  // Mid-week the window opens now rather than re-including Monday morning; on a weekend it
  // opens on the Monday itself.
  const midweek = dateWindow("workweek", "", "", thursday);
  assert.equal(midweek.start.toISOString(), thursday.toISOString());
  assert.equal(midweek.label, "Work week");
  const saturday = dateWindow("workweek", "", "", new Date(2026, 7, 29, 12, 0, 0));
  assert.ok(saturday.start > new Date(2026, 7, 29, 12, 0, 0));
  // Exclusive end: Friday is included, Saturday is not.
  assert.equal(saturday.end.getDay(), 6);
});

test("the next-week windows name the period after the one beside them, never the same days", () => {
  // Thursday Aug 27 2026: this week is Aug 24-30, so next week is Aug 31 - Sep 6.
  const thursday = new Date(2026, 7, 27, 12, 0, 0);
  assert.deepEqual(semanticDateRangeKeys("week", "", "", thursday), {
    start: "2026-08-24", end: "2026-08-30",
  });
  assert.deepEqual(semanticDateRangeKeys("nextweek", "", "", thursday), {
    start: "2026-08-31", end: "2026-09-06",
  });
  assert.deepEqual(semanticDateRangeKeys("nextworkweek", "", "", thursday), {
    start: "2026-08-31", end: "2026-09-04",
  });
  assert.deepEqual(semanticDateRangeKeys("nextweekend", "", "", thursday), {
    start: "2026-09-05", end: "2026-09-06",
  });

  // Asked on the Saturday itself: "this weekend" is the one under way, so "next weekend" is the
  // one after it -- and the work week has already rolled forward, so its successor rolls too.
  const saturday = new Date(2026, 7, 29, 12, 0, 0);
  assert.deepEqual(semanticDateRangeKeys("weekend", "", "", saturday), {
    start: "2026-08-29", end: "2026-08-30",
  });
  assert.deepEqual(semanticDateRangeKeys("nextweekend", "", "", saturday), {
    start: "2026-09-05", end: "2026-09-06",
  });
  assert.deepEqual(semanticDateRangeKeys("workweek", "", "", saturday), {
    start: "2026-08-31", end: "2026-09-04",
  });
  assert.deepEqual(semanticDateRangeKeys("nextworkweek", "", "", saturday), {
    start: "2026-09-07", end: "2026-09-11",
  });

  // No pair of windows may ever describe the same days on the same day.
  for (const day of [thursday, saturday, new Date(2026, 7, 30, 12, 0, 0)]) {
    const pairs = [["week", "nextweek"], ["workweek", "nextworkweek"], ["weekend", "nextweekend"]];
    for (const [near, far] of pairs) {
      assert.notDeepEqual(
        semanticDateRangeKeys(near, "", "", day),
        semanticDateRangeKeys(far, "", "", day),
        `${near} and ${far} collided`,
      );
    }
  }

  // Every one of them is entirely in the future, so none clamps its start to "now".
  for (const preset of ["nextweek", "nextworkweek", "nextweekend"]) {
    assert.ok(dateWindow(preset, "", "", thursday).start > thursday, preset);
  }
});
