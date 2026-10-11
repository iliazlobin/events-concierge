import assert from "node:assert/strict";
import { test } from "node:test";
import { registrationSummary, registrationFilterCount, registrationMatchesFilter } from "../lib/registrations.ts";

const counts = { queued: 55, in_progress: 2, needs_input: 3, uncertain: 4, failed: 5, registered: 6, waitlisted: 7, awaiting_approval: 8 };
test("registration summary distinguishes active, confirmed, attention and organizer waiting", () => {
  assert.deepEqual(registrationSummary(counts), { queued: 55, active: 2, registered: 6, attention: 12, waiting: 15 });
  assert.equal(registrationFilterCount(counts, "all"), 90);
  assert.equal(registrationFilterCount(counts, "registered"), 6);
  assert.equal(registrationFilterCount(counts, "attention"), 12);
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
