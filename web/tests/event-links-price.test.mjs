import assert from "node:assert/strict";
import test from "node:test";

import {
  eventLocationLabel,
  eventPageUrl,
  googleCalendarUrl,
  googleMapsUrl,
} from "../lib/event-links.ts";
import { formatEventPrice } from "../lib/event-price.ts";

function event(overrides = {}) {
  return {
    title: "Night Market & Music",
    start_at: "2026-08-02T00:30:00Z",
    end_at: "2026-08-02T02:00:00Z",
    venue_name: "Ferry Building",
    city: "San Francisco",
    description: "Food stalls\n  and live music.",
    latitude: 37.7955,
    longitude: -122.3937,
    registration_urls: ["https://events.example.test/night-market?ref=calendar"],
    sources: [],
    price_status: "paid",
    price_min_cents: null,
    price_max_cents: null,
    price_currency: null,
    ...overrides,
  };
}

test("Google Calendar links preserve timing, title, location, and safe event context", () => {
  const href = googleCalendarUrl(event());
  assert.ok(href);

  const url = new URL(href);
  assert.equal(url.origin, "https://calendar.google.com");
  assert.equal(url.pathname, "/calendar/render");
  assert.equal(url.searchParams.get("action"), "TEMPLATE");
  assert.equal(url.searchParams.get("text"), "Night Market & Music");
  assert.equal(
    url.searchParams.get("dates"),
    "20260802T003000Z/20260802T020000Z",
  );
  assert.equal(url.searchParams.get("location"), "Ferry Building, San Francisco");
  assert.equal(
    url.searchParams.get("details"),
    "Food stalls and live music.\n\n"
      + "Event page: https://events.example.test/night-market?ref=calendar",
  );
});

test("Google Calendar links use a one-hour fallback and reject invalid starts", () => {
  const fallback = googleCalendarUrl(event({
    start_at: "2026-08-02T00:30:00Z",
    end_at: null,
  }));
  assert.ok(fallback);
  assert.equal(
    new URL(fallback).searchParams.get("dates"),
    "20260802T003000Z/20260802T013000Z",
  );
  assert.equal(googleCalendarUrl(event({ start_at: "not-a-date" })), null);
});

test("event-page URLs allow only HTTP links and fall through to source evidence", () => {
  assert.equal(
    eventPageUrl(event({
      registration_urls: ["javascript:alert(1)"],
      sources: [{ registration_url: "https://provider.example.test/event/42" }],
    })),
    "https://provider.example.test/event/42",
  );
  assert.equal(
    eventPageUrl(event({
      registration_urls: ["data:text/html,unsafe"],
      sources: [],
    })),
    null,
  );
});

test("Google Maps prefers valid coordinates and falls back to venue and city", () => {
  const coordinates = googleMapsUrl(event());
  assert.ok(coordinates);
  assert.equal(
    new URL(coordinates).searchParams.get("query"),
    "37.7955,-122.3937",
  );

  const namedLocation = googleMapsUrl(event({
    latitude: null,
    longitude: null,
    city: "sanfrancisco",
  }));
  assert.ok(namedLocation);
  assert.equal(
    new URL(namedLocation).searchParams.get("query"),
    "Ferry Building, San Francisco",
  );

  assert.equal(
    googleMapsUrl(event({
      latitude: 200,
      longitude: -122,
      venue_name: null,
      city: null,
    })),
    null,
  );
});

test("coordinate-only map links use a truthful visible label", () => {
  assert.equal(
    eventLocationLabel(event({
      venue_name: null,
      city: null,
    })),
    "Mapped location",
  );
  assert.equal(
    eventLocationLabel(event({
      venue_name: null,
      city: null,
      latitude: null,
      longitude: null,
    })),
    "Location TBA",
  );
});

test("paid events show exact amounts and ranges from API cents", () => {
  assert.equal(
    formatEventPrice(event({
      price_min_cents: 2_500,
      price_max_cents: 2_500,
      price_currency: "usd",
    })),
    "$25.00",
  );
  assert.equal(
    formatEventPrice(event({
      price_min_cents: 1_850,
      price_max_cents: 1_850,
      price_currency: "EUR",
    })),
    "€18.50",
  );
  assert.equal(
    formatEventPrice(event({
      price_min_cents: 2_500,
      price_max_cents: 5_000,
      price_currency: "USD",
    })),
    "$25.00–$50.00",
  );
});

test("paid events fall back truthfully for absent, partial, or invalid ranges", () => {
  const invalidRanges = [
    { price_min_cents: null, price_max_cents: null, price_currency: null },
    { price_min_cents: 2_500, price_max_cents: null, price_currency: "USD" },
    { price_min_cents: 5_000, price_max_cents: 2_500, price_currency: "USD" },
    { price_min_cents: 0, price_max_cents: 2_500, price_currency: "USD" },
    { price_min_cents: 2_500.5, price_max_cents: 5_000, price_currency: "USD" },
    {
      price_min_cents: 2_500,
      price_max_cents: 100_000_001,
      price_currency: "USD",
    },
    { price_min_cents: 2_500, price_max_cents: 5_000, price_currency: "ZZZ" },
  ];
  for (const range of invalidRanges) {
    assert.equal(formatEventPrice(event(range)), "Paid");
  }
});

test("free and unknown status remain authoritative over range fields", () => {
  assert.equal(
    formatEventPrice(event({
      price_status: "free",
      price_min_cents: 2_500,
      price_max_cents: 5_000,
      price_currency: "USD",
    })),
    "Free",
  );
  assert.equal(
    formatEventPrice(event({
      price_status: "unknown",
      price_min_cents: 2_500,
      price_max_cents: 5_000,
      price_currency: "USD",
    })),
    null,
  );
});
