import assert from "node:assert/strict";
import test from "node:test";

import {
  adminSourceEnabledTargets,
  selectableAdminSources,
} from "../lib/admin-source-bulk.ts";
import {
  adminSourceIsRetired,
  adminSourceLifecycleLabel,
  adminSourceRetirementDescription,
} from "../lib/admin-source-lifecycle.ts";

const source = (overrides = {}) => ({
  source_key: "sample-source",
  source_revision: 7,
  display_name: "Sample source",
  publisher: "Sample",
  mode: "sample_json",
  region: "bay-area",
  seed_url: "https://example.com/events",
  enabled: false,
  retired_at: null,
  retired_reason: null,
  superseded_by_source_key: null,
  review_status: "reviewed",
  effective_status: "disabled",
  due: false,
  last_succeeded_at: null,
  next_due_at: null,
  event_count: 0,
  latest_run: null,
  ...overrides,
});

test("bulk source selection excludes sources that need owner review or were superseded", () => {
  const reviewed = source();
  const unreviewed = source({
    source_key: "unreviewed-source",
    review_status: "unreviewed",
  });
  const expired = source({
    source_key: "expired-source",
    review_status: "expired",
  });
  const superseded = source({
    source_key: "alameda-county-library-fremont-events",
    effective_status: "retired",
    retired_at: "2026-08-01T20:00:00Z",
    retired_reason: "superseded_by_aggregate_source",
    superseded_by_source_key: "alameda-county-library-all-physical-branches-events",
  });

  assert.deepEqual(
    selectableAdminSources([reviewed, unreviewed, expired, superseded]),
    [reviewed],
  );
});

test("retired source lifecycle is terminal and names an authoritative replacement", () => {
  const retired = source({
    source_key: "old-source",
    effective_status: "retired",
    retired_at: "2026-08-01T20:00:00Z",
    retired_reason: "superseded_by_aggregate_source",
    superseded_by_source_key: "aggregate-source",
  });

  assert.equal(adminSourceIsRetired(retired), true);
  assert.equal(adminSourceLifecycleLabel(retired), "Superseded");
  assert.match(adminSourceRetirementDescription(retired), /authoritative/);
  assert.deepEqual(selectableAdminSources([retired]), []);
  assert.deepEqual(adminSourceEnabledTargets([retired], true), []);
  assert.deepEqual(adminSourceEnabledTargets([retired], false), []);
});

test("bulk enabled targets include only state changes and preserve optimistic revisions", () => {
  const paused = source();
  const enabled = source({
    source_key: "enabled-source",
    source_revision: 11,
    enabled: true,
    effective_status: "active",
  });

  assert.deepEqual(adminSourceEnabledTargets([paused, enabled], true), [
    { source_key: "sample-source", expected_revision: 7 },
  ]);
  assert.deepEqual(adminSourceEnabledTargets([paused, enabled], false), [
    { source_key: "enabled-source", expected_revision: 11 },
  ]);

  assert.deepEqual(adminSourceEnabledTargets([
    source({ source_key: "smcl-millbrae-events" }),
  ], true), []);
});
