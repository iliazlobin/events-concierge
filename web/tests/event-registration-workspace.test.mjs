import assert from "node:assert/strict";
import { test } from "node:test";
import { registrationActivity, registrationProgress, registrationSummary, registrationFilterCount, registrationMatchesFilter } from "../lib/registrations.ts";

const counts = { queued: 55, in_progress: 2, needs_input: 3, uncertain: 4, failed: 5, registered: 6, waitlisted: 7, awaiting_approval: 8 };
test("registration summary distinguishes active, confirmed, attention and organizer waiting", () => {
  assert.deepEqual(registrationSummary(counts), { queued: 55, active: 2, registered: 6, attention: 12, waiting: 15 });
  assert.equal(registrationFilterCount(counts, "all"), 90);
  assert.equal(registrationFilterCount(counts, "registered"), 6);
  assert.equal(registrationFilterCount(counts, "attention"), 12);
});
test("activity shows only saved timestamps and does not invent a browser history", () => {
  const created_at = "2030-06-01T08:00:00Z", updated_at = "2030-06-01T08:30:00Z";
  // Updating the registration account changes updated_at while still queued; it is not Muse activity.
  assert.deepEqual(registrationActivity({ status: "queued", created_at, updated_at }), [
    { status: "queued", at: created_at },
  ]);
  assert.deepEqual(registrationActivity({ status: "in_progress", created_at, updated_at }), [
    { status: "queued", at: created_at }, { status: "in_progress", at: updated_at },
  ]);
  // Earlier claims and input requests cannot be reconstructed from a completed snapshot.
  assert.deepEqual(registrationActivity({ status: "registered", created_at, updated_at, version: 6 }), [
    { status: "queued", at: created_at }, { status: "registered", at: updated_at },
  ]);
  assert.match(registrationProgress.queued, /Waiting for Muse/);
  assert.match(registrationProgress.in_progress, /Waiting for a result/);
  assert.match(registrationProgress.uncertain, /Check provider confirmation before retrying/);
});
test("every registration status belongs to a visible category without conflating organizer approval with registration", () => {
  const categories = ["queued", "active", "registered", "attention", "waiting"];
  for (const status of Object.keys(counts)) {
    const matching = categories.filter(filter => registrationMatchesFilter({ status }, filter));
    assert.equal(matching.length, 1, status);
    assert.ok(registrationMatchesFilter({ status }, "all"));
  }
  assert.equal(registrationMatchesFilter({status: "awaiting_approval"}, "registered"), false);
  assert.equal(registrationMatchesFilter({status: "waitlisted"}, "registered"), false);
});
