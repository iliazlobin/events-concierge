import assert from "node:assert/strict";
import test from "node:test";

import {
  dayTrackLabel,
  eventDayKey,
  groupVisibleEventsByDay,
  mapDayModel,
  stepDay,
} from "../lib/map-days.ts";

/** Local-time literals on purpose: the day a reader sees is their day, not UTC's. */
function event(id, startAt) {
  return { canonical_event_id: id, start_at: startAt, title: id };
}

const WEEK = [
  event("a", "2026-08-25T18:00:00"),
  event("b", "2026-08-25T23:50:00"),
  event("c", "2026-08-26T00:10:00"),
  event("d", "2026-08-26T17:30:00"),
  event("e", "2026-08-26T19:00:00"),
  event("f", "2026-08-29T11:00:00"),
];

test("days come from the event's own local calendar day, not from UTC", () => {
  const model = mapDayModel(WEEK, WEEK.map((item) => item.canonical_event_id), null);
  assert.deepEqual(model.keys, ["2026-08-25", "2026-08-26", "2026-08-29"]);
  // 23:50 and 00:10 are ten minutes apart and belong to different days.
  assert.equal(eventDayKey(WEEK[1]), "2026-08-25");
  assert.equal(eventDayKey(WEEK[2]), "2026-08-26");
  assert.deepEqual(model.cells.map((cell) => cell.total), [2, 3, 1]);
});

test("cells carry the discontinuity and the month boundary they sit on", () => {
  const model = mapDayModel(WEEK, [], null);
  assert.equal(model.cells[0].gapBefore, 0);
  assert.equal(model.cells[1].gapBefore, 0);
  // Aug 26 -> Aug 29 skips two whole days with no mapped events.
  assert.equal(model.cells[2].gapBefore, 2);
  assert.equal(model.cells.every((cell) => cell.monthLabel === null), true);

  const acrossMonths = mapDayModel(
    [event("x", "2026-08-31T10:00:00"), event("y", "2026-09-01T10:00:00")],
    [],
    null,
  );
  assert.equal(acrossMonths.cells[0].monthLabel, null);
  assert.equal(acrossMonths.cells[1].monthLabel, "SEP");
  assert.equal(acrossMonths.cells[1].gapBefore, 0);
});

test("panning changes the counts and never the track's own geometry", () => {
  const all = mapDayModel(WEEK, WEEK.map((item) => item.canonical_event_id), null);
  const pinhole = mapDayModel(WEEK, ["d"], null);

  // This is the invariant that stops a future refactor from deriving cells from the
  // viewport and making the control reflow under the reader's cursor mid-drag.
  assert.deepEqual(pinhole.keys, all.keys);
  assert.deepEqual(
    pinhole.cells.map((cell) => cell.total),
    all.cells.map((cell) => cell.total),
  );
  assert.equal(pinhole.maxTotal, all.maxTotal);
  assert.deepEqual(pinhole.cells.map((cell) => cell.inView), [0, 1, 0]);
  assert.equal(pinhole.totalInView, 1);
});

test("the active day reports what is here and what is off screen", () => {
  const none = mapDayModel(WEEK, ["a", "d"], null);
  assert.equal(none.activeInView, 0);
  assert.equal(none.activeTotal, 0);
  assert.equal(none.maxTotal, 3);

  const active = mapDayModel(WEEK, ["a", "d"], "2026-08-26");
  assert.equal(active.activeInView, 1);
  assert.equal(active.activeTotal, 3);
  assert.equal(active.activeTotal - active.activeInView, 2);
});

test("a single day yields a full-weight cell and no track", () => {
  const model = mapDayModel([event("a", "2026-08-25T18:00:00")], ["a"], null);
  assert.equal(model.cells.length, 1);
  assert.equal(model.cells[0].total / model.maxTotal, 1);
});

test("unparsable starts are dropped instead of minting a garbage day", () => {
  const model = mapDayModel(
    [event("bad", "not-a-date"), event("good", "2026-08-26T09:00:00")],
    ["bad", "good"],
    null,
  );
  assert.equal(eventDayKey(event("bad", "not-a-date")), null);
  assert.deepEqual(model.keys, ["2026-08-26"]);
  assert.equal(model.keys.some((key) => /NaN/.test(key)), false);
});

test("stepping walks only days that exist and clamps at both ends", () => {
  const keys = ["2026-08-25", "2026-08-26", "2026-08-29"];
  assert.equal(stepDay(keys, "2026-08-25", 1), "2026-08-26");
  // Hopping a three-day hole costs one keystroke and never lands on nothing.
  assert.equal(stepDay(keys, "2026-08-26", 1), "2026-08-29");
  assert.equal(stepDay(keys, "2026-08-29", 1), "2026-08-29");
  assert.equal(stepDay(keys, "2026-08-25", -1), "2026-08-25");
  assert.equal(stepDay(keys, null, 1), "2026-08-25");
  assert.equal(stepDay(keys, null, -1), "2026-08-29");
  assert.equal(stepDay(keys, "2026-12-01", 1), "2026-08-25");
  assert.equal(stepDay([], "2026-08-25", 1), null);
});

test("rail sections are chronological whatever order the catalog delivered", () => {
  const shuffled = [WEEK[5], WEEK[2], WEEK[0], WEEK[4], WEEK[1], WEEK[3]];
  const groups = groupVisibleEventsByDay(shuffled);
  assert.deepEqual(groups.map((group) => group.key), [
    "2026-08-25",
    "2026-08-26",
    "2026-08-29",
  ]);
  assert.deepEqual(
    groups.flatMap((group) => group.events.map((item) => item.canonical_event_id)),
    ["a", "b", "c", "d", "e", "f"],
  );
});

test("day labels read the same in the track and in the rail heading", () => {
  assert.match(dayTrackLabel("2026-08-26"), /^[A-Z]{3} · [A-Z]{3} 26$/);
});
