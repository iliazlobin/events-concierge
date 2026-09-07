import assert from "node:assert/strict";
import test from "node:test";

import {
  displayedRunStatus,
  isFailedRun,
  presentAdminRuns,
  runStatusLabel,
} from "../lib/admin-presentation.ts";

function run(sourceKey, runKey, status, startedAt, error = null, truth = {}) {
  return {
    source_key: sourceKey,
    display_name: sourceKey,
    run_key: runKey,
    status,
    started_at: startedAt,
    completed_at: startedAt,
    candidate_count: status === "succeeded" ? 12 : 0,
    canonical_count: status === "succeeded" ? 12 : 0,
    error,
    attempt_count: 1,
    duration_ms: 100,
    source_revision: 1,
    release_revision: "revision",
    image_digest: null,
    provenance_status: "claim_recorded",
    trigger: "cadence_or_manual",
    ...truth,
  };
}

const RUNS = [
  run("recovered", "failed-old", "failed", "2026-07-26T09:00:00Z", "provider_timeout"),
  run("currently-failed", "success-old", "succeeded", "2026-07-25T09:00:00Z"),
  run("recovered", "success-new", "succeeded", "2026-07-28T09:00:00Z"),
  run("currently-failed", "failed-new", "failed", "2026-07-28T10:00:00Z", "parse_error"),
  run("still-running", "failed-old", "failed", "2026-07-26T11:00:00Z", "provider_timeout"),
  run("still-running", "running-new", "running", "2026-07-28T11:00:00Z"),
];

test("current view returns only the newest run for each source", () => {
  const rows = presentAdminRuns(RUNS, "current", "");

  assert.deepEqual(
    rows.map(({ run: value }) => value.run_key),
    ["running-new", "failed-new", "success-new"],
  );
  assert.equal(rows.every(({ disposition }) => disposition === "current"), true);
});

test("current failure filtering happens after selecting each source's latest run", () => {
  const rows = presentAdminRuns(RUNS, "current", "failed");

  assert.deepEqual(
    rows.map(({ run: value }) => `${value.source_key}:${value.run_key}`),
    ["currently-failed:failed-new"],
  );
});

test("history marks only failed attempts followed by a newer success as resolved", () => {
  const rows = presentAdminRuns(RUNS, "history", "failed");
  const states = Object.fromEntries(
    rows.map(({ run: value, disposition }) => [
      `${value.source_key}:${value.run_key}`,
      disposition,
    ]),
  );

  assert.deepEqual(states, {
    "still-running:failed-old": "historical",
    "recovered:failed-old": "resolved",
    "currently-failed:failed-new": "current",
  });
});

test("authoritative latest and resolution flags override timestamp inference", () => {
  const rows = [
    run(
      "flagged",
      "failure-with-later-timestamp",
      "failed",
      "2026-07-29T09:00:00Z",
      "provider_timeout",
      { is_latest_for_source: false, resolved_by_newer_success: true },
    ),
    run(
      "flagged",
      "authoritative-current",
      "succeeded",
      "2026-07-28T09:00:00Z",
      null,
      { is_latest_for_source: true, resolved_by_newer_success: false },
    ),
    run(
      "not-resolved",
      "failure-old",
      "failed",
      "2026-07-26T09:00:00Z",
      "parse_error",
      { is_latest_for_source: false, resolved_by_newer_success: false },
    ),
    run(
      "not-resolved",
      "success-new",
      "succeeded",
      "2026-07-28T10:00:00Z",
      null,
      { is_latest_for_source: true, resolved_by_newer_success: false },
    ),
  ];

  assert.deepEqual(
    presentAdminRuns(rows, "current", "").map(({ run: value }) => value.run_key),
    ["success-new", "authoritative-current"],
  );
  assert.deepEqual(
    Object.fromEntries(
      presentAdminRuns(rows, "history", "failed").map(({ run: value, disposition }) => [
        value.run_key,
        disposition,
      ]),
    ),
    {
      "failure-with-later-timestamp": "resolved",
      "failure-old": "historical",
    },
  );
});

test("pacing deferrals present as paused and stay out of failed-only views", () => {
  const deferral = run(
    "paced",
    "pacer-deferred",
    "failed",
    "2026-07-28T09:00:00Z",
    "pacer_deferred",
    { is_latest_for_source: true, resolved_by_newer_success: true },
  );

  assert.equal(displayedRunStatus(deferral), "paused");
  assert.equal(runStatusLabel(deferral), "Deferred / paused");
  assert.equal(isFailedRun(deferral), false);
  assert.deepEqual(presentAdminRuns([deferral], "current", "failed"), []);
  assert.deepEqual(
    presentAdminRuns([deferral], "current", "paused").map(({ run: value }) => value.run_key),
    ["pacer-deferred"],
  );

  const history = presentAdminRuns(
    [
      { ...deferral, is_latest_for_source: false },
      run("paced", "success-new", "succeeded", "2026-07-29T09:00:00Z", null, {
        is_latest_for_source: true,
      }),
    ],
    "history",
    "",
  );
  assert.equal(
    history.find(({ run: value }) => value.run_key === "pacer-deferred").disposition,
    "historical",
  );
  assert.deepEqual(
    presentAdminRuns(history.map(({ run: value }) => value), "history", "failed"),
    [],
  );
});
