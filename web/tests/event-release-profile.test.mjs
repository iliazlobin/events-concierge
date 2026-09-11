import assert from "node:assert/strict";
import test from "node:test";
import { initialCatalogFilters } from "../lib/catalog-filters.ts";
import { createConsumerHistorySnapshot, consumerHistorySnapshotFromUrl, consumerHistoryUrl } from "../lib/consumer-history.ts";
import { releaseHistorySnapshot, releaseHome, releaseProfile, releaseSettingsAllowed, releaseViewAllowed } from "../lib/release-profile.ts";

test("only a successful known runtime config enables the full profile", () => {
  assert.equal(releaseProfile(null), "discovery");
  assert.equal(releaseProfile({ release_profile: "unexpected" }), "discovery");
  assert.equal(releaseProfile({ release_profile: "discovery" }), "discovery");
  assert.equal(releaseProfile({ release_profile: "full" }), "full");
  assert.equal(releaseProfile({}), "full", "older API configs remain compatible");
  assert.equal(releaseHome("discovery"), "events");
  assert.equal(releaseHome("full"), "chat");
});

test("discovery sanitizes deferred URL and history views while preserving catalog filters", () => {
  for (const view of ["chat", "entities"]) {
    const raw = consumerHistorySnapshotFromUrl(`https://example.test/?view=${view}&q=music&topic=jazz&entity=hidden&release_profile=full`, initialCatalogFilters());
    const safe = releaseHistorySnapshot(raw, "discovery");
    assert.equal(safe.view, "events");
    assert.equal(safe.selectedEntityId, null);
    assert.equal(safe.filters.query, "music");
    assert.deepEqual(safe.filters.topics, ["jazz"]);
    const url = new URL(consumerHistoryUrl(safe, "https://example.test/"), "https://example.test");
    assert.equal(url.searchParams.get("view"), "events");
    assert.equal(url.searchParams.get("entity"), null);
    assert.equal(releaseHistorySnapshot(raw, "full"), raw);
  }
});

test("discovery keeps catalog navigation and calendar state, and hides automated activity", () => {
  for (const view of ["events", "map", "calendar"]) {
    const snapshot = createConsumerHistorySnapshot(view, initialCatalogFilters(), "event-id", "week");
    assert.deepEqual(releaseHistorySnapshot(snapshot, "discovery"), snapshot);
    assert.equal(releaseViewAllowed(view, "discovery"), true);
  }
  for (const view of ["chat", "entities"]) assert.equal(releaseViewAllowed(view, "discovery"), false);
  assert.equal(releaseSettingsAllowed("/settings/activity", "discovery"), false);
  assert.equal(releaseSettingsAllowed("/settings/activity", "full"), true);
  assert.equal(releaseSettingsAllowed("/settings/security", "discovery"), false);
  assert.equal(releaseSettingsAllowed("/settings/security", "full"), true);
  assert.equal(releaseSettingsAllowed("/settings/api-keys", "discovery"), false);
  assert.equal(releaseSettingsAllowed("/settings/api-keys", "full"), true);
  assert.equal(releaseSettingsAllowed("/settings/saved-filters", "discovery"), true);
});
