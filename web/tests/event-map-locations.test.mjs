import assert from "node:assert/strict";
import test from "node:test";

import { eventMapLocation, mapMarkerGroups } from "../lib/map-locations.ts";
import { eventMapCoordinate, eventsInMapBounds, partitionMapEvents } from "../lib/map-viewport.ts";

function techWeek(venue, extra = {}) {
  return {
    latitude: null, longitude: null, city: "sanfrancisco", venue_name: venue,
    source_keys: ["tech-week-sf-2026"], ...extra,
  };
}

test("known publisher areas are display-only and provider coordinates take precedence", () => {
  const event = techWeek(" SOMA ");
  const original = structuredClone(event);
  assert.deepEqual(eventMapLocation(event), {
    coordinate: [-122.4081, 37.7801], precision: "area",
    areaKey: "sf:South of Market", label: "South of Market",
  });
  assert.deepEqual(eventMapCoordinate(event), [-122.4081, 37.7801]);
  assert.deepEqual(event, original, "The canonical event must not acquire synthetic coordinates");
  assert.deepEqual(eventMapLocation(techWeek("SOMA", { latitude: 37.77, longitude: -122.4 })), {
    coordinate: [-122.4, 37.77], precision: "venue",
  });
  assert.deepEqual(eventMapLocation(techWeek("SOMA", { latitude: 0, longitude: 0 })), {
    coordinate: [0, 0], precision: "venue",
  });
});

test("fallback requires a reviewed source, matching city and an exact known area", () => {
  for (const venue of ["Virtual (SF)", "Other", "Location TBA", "SOMA studio", "SOMA / Virtual (SF)", "constructor", "__proto__", null]) {
    assert.equal(eventMapLocation(techWeek(venue)), null, venue);
  }
  for (const extra of [
    { city: "losangeles" }, { city: null }, { source_keys: ["luma-genai-sf"] },
    { source_keys: ["tech-week-la-2026"] }, { source_keys: ["tech-week-sf-2027"] },
    { latitude: 37, longitude: null }, { latitude: 91, longitude: -122 },
    { latitude: Number.NaN, longitude: -122 },
  ]) assert.equal(eventMapLocation(techWeek("SOMA", extra)), null);
  assert.equal(eventMapLocation(techWeek("SOMA", {
    source_keys: [], city: "San Francisco",
    sources: [{ source_key: "tech-week-sf-2026" }],
  }))?.precision, "area");
});

test("the deployed 72-event page maps 60 area-only events and keeps 12 unlocated", () => {
  // Anonymous location distribution from the public catalog, verified 2026-10-06.
  const distribution = {
    SOMA: 16, "Virtual (SF)": 7, Embarcadero: 6, "FiDi (SF)": 10, Other: 5,
    Marina: 3, "Jackson Square": 4, "Downtown (SF)": 7, "Duboce Triangle": 1,
    "Salesforce Park": 1, "Civic Center": 1, "Golden Gate Park": 2,
    Dogpatch: 1, Mission: 4, "Hayes Valley": 1, "Nob Hill": 1, "Union Square (SF)": 2,
  };
  const events = Object.entries(distribution).flatMap(([venue, count]) => (
    Array.from({ length: count }, () => techWeek(venue))
  ));
  const original = structuredClone(events);
  const { mapped, unmapped } = partitionMapEvents(events);
  assert.equal(events.length, 72);
  assert.equal(mapped.length, 60);
  assert.equal(unmapped.length, 12);
  assert.equal(mapMarkerGroups(mapped).length, 14);
  assert.equal(mapMarkerGroups(mapped).reduce((sum, group) => sum + group.events.length, 0), 60);
  assert.deepEqual(events, original);
  assert.equal(eventsInMapBounds(events, { contains: () => true }).length, 60);
});

test("area aliases share one count marker while exact pins remain independent", () => {
  const events = [techWeek("Downtown (SF)"), techWeek("Union Square (SF)"),
    techWeek("SOMA", { latitude: 37.77, longitude: -122.4 }),
    techWeek("SOMA", { latitude: 37.77, longitude: -122.4 }), techWeek("Other")];
  const groups = mapMarkerGroups(events);
  assert.deepEqual(groups.map((group) => group.events.length), [2, 1, 1]);
  assert.equal(groups[0].location.precision, "area");
  assert.equal(groups[1].location.precision, "venue");
  assert.deepEqual(groups.flatMap((group) => group.events), events.slice(0, 4));
});
