import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalReadiness,
  catalogEnrichmentSummary,
  catalogMetadataGroups,
} from "../lib/admin-catalog-coverage.ts";

function event(overrides = {}) {
  return {
    canonical_event_id: "33333333-3333-4333-8333-333333333333",
    title: "Canonical event",
    start_at: "2026-08-01T17:00:00Z",
    end_at: "2026-08-01T19:00:00Z",
    venue_name: "Civic Hall",
    city: "sanfrancisco",
    latitude: 37.77,
    longitude: -122.42,
    description: "Normalized public description.",
    description_length: 30,
    price_status: "unknown",
    price_min_cents: null,
    price_max_cents: null,
    price_currency: null,
    event_status: "scheduled",
    normalizer_version: 2,
    merge_version: 3,
    source_event_id: "provider-42",
    registration_url: "https://events.example.test/provider-42",
    last_seen_at: "2026-07-31T18:30:00Z",
    refresh_run_key: "admin:city-events:20260731T183000Z",
    quality_issues: [],
    organizer_name: null,
    host_names: [],
    speaker_names: [],
    partner_names: [],
    entity_profiles: [],
    attendance_count: null,
    registration_status: "unknown",
    ...overrides,
  };
}

function fact(groups, groupId, factId) {
  return groups
    .find((group) => group.id === groupId)
    .facts.find((item) => item.id === factId);
}

test("canonical readiness reports readiness or concrete gaps without a denominator", () => {
  assert.deepEqual(canonicalReadiness(event()), {
    ready: true,
    gapCount: 0,
    label: "Discovery ready",
    detail: "Canonical discovery signals are available",
  });

  const gaps = canonicalReadiness(event({
    quality_issues: ["missing_end_time", "missing_geo"],
  }));
  assert.equal(gaps.ready, false);
  assert.equal(gaps.gapCount, 2);
  assert.equal(gaps.label, "2 discovery gaps");
  assert.equal(gaps.detail, "Missing end time, coordinates");
  assert.doesNotMatch(gaps.label, /\d+\/\d+/);
});

test("optional metadata distinguishes present, unknown, and not applicable", () => {
  const freeGroups = catalogMetadataGroups(event({ price_status: "free" }));
  assert.deepEqual(fact(freeGroups, "access", "price_status"), {
    id: "price_status",
    label: "Price classification",
    state: "present",
    value: "Free",
  });
  assert.equal(fact(freeGroups, "access", "exact_price").state, "not_applicable");
  assert.equal(fact(freeGroups, "people", "organizer").state, "unknown");
  assert.equal(fact(freeGroups, "people", "profiles").state, "not_applicable");
  assert.equal(fact(freeGroups, "people", "attendance").state, "unknown");
});

test("exact price and only safe source-provided direct profiles become present metadata", () => {
  const groups = catalogMetadataGroups(event({
    price_status: "paid",
    price_min_cents: 2_500,
    price_max_cents: 5_000,
    price_currency: "USD",
    organizer_name: "Example Org",
    host_names: ["Ada Host"],
    entity_profiles: [
      {
        name: "Example Org",
        role: "organizer",
        kind: "organization",
        profile_url: "https://www.linkedin.com/company/example-org",
      },
      {
        name: "Ada Host",
        role: "host",
        kind: "person",
        profile_url: "javascript:alert(1)",
      },
      {
        name: "Example Org",
        role: "organizer",
        kind: "person",
        profile_url: "https://www.linkedin.com/in/example-org",
      },
    ],
    attendance_count: 80,
    registration_status: "waitlist",
  }));

  assert.equal(fact(groups, "access", "exact_price").value, "$25.00–$50.00");
  assert.equal(fact(groups, "access", "registration_status").value, "Waitlist");
  const profiles = fact(groups, "people", "profiles");
  assert.equal(profiles.state, "present");
  assert.deepEqual(profiles.links, [{
    label: "Example Org",
    href: "https://www.linkedin.com/company/example-org",
  }]);
  assert.equal(fact(groups, "people", "attendance").value, "80 going");
  assert.deepEqual(catalogEnrichmentSummary(event({
    price_status: "paid",
    price_min_cents: 2_500,
    price_max_cents: 5_000,
    price_currency: "USD",
    organizer_name: "Example Org",
    registration_status: "waitlist",
    attendance_count: 80,
  })), ["$25.00–$50.00", "Waitlist", "1 named entity"]);
});

test("paid classification remains truthful when an exact amount is unpublished", () => {
  const exact = fact(
    catalogMetadataGroups(event({ price_status: "paid" })),
    "access",
    "exact_price",
  );
  assert.equal(exact.state, "unknown");
  assert.equal(exact.value, "Amount not published");
});
