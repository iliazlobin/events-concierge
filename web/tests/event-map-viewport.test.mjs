import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_MAP_VIEWPORT,
  cityMapViewport,
  eventMapCoordinate,
  eventsInMapBounds,
  mapViewportFor,
} from "../lib/map-viewport.ts";
import { CATALOG_CITY_VALUES } from "../lib/presentation.ts";

test("known city names resolve to deterministic local viewports", () => {
  assert.deepEqual(cityMapViewport("oakland"), {
    kind: "center",
    center: [-122.2711, 37.8044],
    zoom: 11,
  });
  assert.deepEqual(cityMapViewport("San Francisco"), {
    kind: "center",
    center: [-122.4194, 37.7749],
    zoom: 11,
  });
  assert.deepEqual(cityMapViewport(""), DEFAULT_MAP_VIEWPORT);
  assert.deepEqual(cityMapViewport("Unknown place"), DEFAULT_MAP_VIEWPORT);
});

test("every catalog city has a specific empty-result center", () => {
  for (const city of CATALOG_CITY_VALUES) {
    assert.notDeepEqual(cityMapViewport(city), DEFAULT_MAP_VIEWPORT, city);
  }
});

test("mapped events take precedence over the selected-city fallback", () => {
  assert.deepEqual(
    mapViewportFor(
      [
        { latitude: 37.8, longitude: -122.27 },
        { latitude: 37.87, longitude: -122.28 },
      ],
      "sanfrancisco",
    ),
    {
      kind: "bounds",
      coordinates: [
        [-122.27, 37.8],
        [-122.28, 37.87],
      ],
    },
  );
});

test("one mapped event uses a stable close center instead of degenerate bounds", () => {
  assert.deepEqual(
    mapViewportFor(
      [{ latitude: 37.8, longitude: -122.27 }],
      "sanfrancisco",
    ),
    {
      kind: "center",
      center: [-122.27, 37.8],
      zoom: 12,
    },
  );
});

test("zero mapped events recenter on the selected city", () => {
  assert.deepEqual(
    mapViewportFor(
      [
        { latitude: null, longitude: null },
        { latitude: 95, longitude: -122 },
      ],
      "oakland",
    ),
    {
      kind: "center",
      center: [-122.2711, 37.8044],
      zoom: 11,
    },
  );
});

test("zero-result additive locations fit their combined fallback bounds", () => {
  assert.deepEqual(
    mapViewportFor([], ["sanfrancisco", "santamonica"]),
    {
      kind: "bounds",
      coordinates: [
        [-122.4194, 37.7749],
        [-118.4912, 34.0195],
      ],
    },
  );
  assert.deepEqual(
    mapViewportFor([], [], ["manhattan"]),
    {
      kind: "bounds",
      coordinates: [
        [-74.0479, 40.6829],
        [-73.9067, 40.879],
      ],
    },
  );
});

test("coordinate validation keeps zeroes and rejects incomplete or out-of-range pairs", () => {
  assert.deepEqual(eventMapCoordinate({ latitude: 0, longitude: 0 }), [0, 0]);
  assert.equal(eventMapCoordinate({ latitude: null, longitude: -122 }), null);
  assert.equal(eventMapCoordinate({ latitude: 37, longitude: null }), null);
  assert.equal(eventMapCoordinate({ latitude: 91, longitude: 0 }), null);
  assert.equal(eventMapCoordinate({ latitude: 0, longitude: -181 }), null);
});

test("visible map events follow live bounds and discard unmapped records", () => {
  const events = [
    { id: "inside", latitude: 37.78, longitude: -122.42 },
    { id: "east", latitude: 37.8, longitude: -121.9 },
    { id: "unmapped", latitude: null, longitude: null },
  ];
  const bounds = {
    contains: ([longitude, latitude]) => (
      longitude >= -122.5
      && longitude <= -122.3
      && latitude >= 37.7
      && latitude <= 37.9
    ),
  };

  assert.deepEqual(
    eventsInMapBounds(events, bounds).map((event) => event.id),
    ["inside"],
  );
});
