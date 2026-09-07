import assert from "node:assert/strict";
import test from "node:test";

import {
  commandEvidence,
  commandExecutionPresentation,
  commandNeedsReview,
  commandOutcomePresentation,
  commandReviewDisposition,
  commandResultSummary,
} from "../lib/admin-presentation.ts";

function command(overrides = {}) {
  return {
    command_id: "command-1",
    action: "refresh_due",
    source_key: null,
    status: "completed",
    requested_at: "2026-07-28T12:00:00Z",
    started_at: "2026-07-28T12:00:01Z",
    completed_at: "2026-07-28T12:00:02Z",
    result: null,
    error_code: null,
    source_revision: null,
    release_revision: null,
    image_digest: null,
    executor_release_revision: null,
    executor_source_revision: null,
    executor_image_digest: null,
    ...overrides,
  };
}

test("completed refresh_due commands surface deferred, failed, and skipped work", () => {
  const value = command({
    result: {
      action: "refresh_due",
      due_sources: 17,
      attempted: 17,
      succeeded: 10,
      queued: 1,
      skipped: 1,
      deferred: 3,
      already_succeeded: 1,
      busy: 0,
      progressed: 0,
      failed: 1,
    },
  });

  assert.equal(commandNeedsReview(value), true);
  assert.equal(
    commandResultSummary(value),
    "11 of 17 sources refreshed",
  );
  const outcome = commandOutcomePresentation(value);
  assert.equal(outcome.tone, "danger");
  assert.equal(outcome.progressPercent, 65);
  assert.deepEqual(
    outcome.metrics.map(({ key, value }) => [key, value]),
    [
      ["succeeded", "10"],
      ["already_succeeded", "1"],
      ["failed", "1"],
      ["deferred", "3"],
      ["skipped", "1"],
      ["queued", "1"],
    ],
  );
  assert.equal(outcome.metrics.some(({ value }) => value === "0"), false);
});

test("a terminal refresh_source outcome can require review despite command completion", () => {
  const value = command({
    action: "refresh_source",
    source_key: "oakland-events",
    result: {
      action: "refresh_source",
      source_key: "oakland-events",
      run_key: "run-1",
      outcome: "deferred",
      candidate_count: 12,
      canonical_count: 10,
    },
  });

  assert.equal(commandNeedsReview(value), true);
  assert.equal(
    commandResultSummary(value),
    "Source refresh deferred",
  );
  assert.deepEqual(commandEvidence(value), [
    ["action", "refresh_source"],
    ["source_key", "oakland-events"],
    ["outcome", "deferred"],
    ["run_key", "run-1"],
    ["candidate_count", 12],
    ["canonical_count", 10],
  ]);
  const outcome = commandOutcomePresentation(value);
  assert.equal(outcome.progressPercent, null);
  assert.deepEqual(outcome.metrics, []);
  assert.deepEqual(outcome.segments, []);
});

test("successful completed commands retain their completed presentation", () => {
  const value = command({
    result: {
      action: "refresh_due",
      due_sources: 2,
      attempted: 2,
      succeeded: 2,
      skipped: 0,
      deferred: 0,
      failed: 0,
    },
  });

  assert.equal(commandNeedsReview(value), false);
  assert.equal(
    commandResultSummary(value),
    "2 sources refreshed",
  );
  assert.equal(commandOutcomePresentation(value).progressPercent, 100);
});

test("nonterminal commands do not claim a needs-review terminal state", () => {
  const value = command({
    status: "running",
    completed_at: null,
    result: { deferred: 4 },
  });

  assert.equal(commandNeedsReview(value), false);
});

test("command presentation never renders arbitrary result keys or invalid values", () => {
  const value = command({
    action: "refresh_source",
    source_key: "oakland-events",
    result: {
      action: "not-the-command-action",
      source_key: "another-source",
      outcome: "provider-secret",
      run_key: "invalid key with spaces",
      candidate_count: "not-a-number",
      provider_payload: "do-not-render",
    },
  });

  assert.deepEqual(commandEvidence(value), []);
  assert.equal(commandResultSummary(value), "No bounded result recorded");
});

test("a no-op fleet pass says no sources were due and renders no fake metrics", () => {
  const value = command({
    result: {
      action: "refresh_due",
      due_sources: 0,
      attempted: 0,
      succeeded: 0,
      failed: 0,
      deferred: 0,
    },
  });

  assert.deepEqual(commandOutcomePresentation(value), {
    tone: "neutral",
    headline: "No sources were due",
    detail: "The cadence check found no eligible source that needed a refresh.",
    progressPercent: null,
    metrics: [],
    segments: [],
  });
});

test("successful source refreshes present catalog yield instead of raw evidence", () => {
  const value = command({
    action: "refresh_source",
    source_key: "luma-sf",
    result: {
      action: "refresh_source",
      source_key: "luma-sf",
      outcome: "succeeded",
      run_key: "run-2",
      candidate_count: 66,
      canonical_count: 65,
    },
  });

  const outcome = commandOutcomePresentation(value);
  assert.equal(outcome.headline, "65 events published");
  assert.equal(outcome.progressPercent, 98);
  assert.deepEqual(
    outcome.metrics.map(({ key, value }) => [key, value]),
    [
      ["candidate_count", "66"],
      ["canonical_count", "65"],
      ["yield", "98%"],
    ],
  );
  assert.deepEqual(
    outcome.segments.map(({ key, value }) => [key, value]),
    [["canonical", 65], ["filtered", 1]],
  );
});

test("candidate-only source output is highlighted as an operator issue", () => {
  const value = command({
    action: "refresh_source",
    source_key: "luma-sf",
    result: {
      action: "refresh_source",
      source_key: "luma-sf",
      outcome: "succeeded",
      candidate_count: 4,
      canonical_count: 0,
    },
  });

  const outcome = commandOutcomePresentation(value);
  assert.equal(outcome.tone, "warning");
  assert.equal(outcome.headline, "No events reached the catalog");
  assert.equal(outcome.progressPercent, 0);
});

test("queued and running commands describe lifecycle without terminal counts", () => {
  const queued = command({ status: "queued", completed_at: null });
  const running = command({
    status: "running",
    started_at: "2026-07-28T12:00:01Z",
    completed_at: null,
  });

  assert.equal(commandOutcomePresentation(queued).headline, "Waiting for a worker");
  assert.equal(commandOutcomePresentation(running).headline, "Refreshing due sources");
  assert.match(
    commandOutcomePresentation(running).detail,
    /recalculating which reviewed sources are due/,
  );
  assert.deepEqual(commandOutcomePresentation(running).metrics, []);
});

test("nonterminal commands explain their bounded execution without inventing live progress", () => {
  const queued = command({ status: "queued", started_at: null, completed_at: null });
  const running = command({
    status: "running",
    started_at: "2026-07-28T12:00:01Z",
    completed_at: null,
  });

  assert.deepEqual(commandExecutionPresentation(queued), {
    headline: "Bounded cadence pass waiting for a worker",
    detail: "No provider work has started. The due-source set will be calculated after a worker claims this durable command.",
    facts: [
      { key: "scope", label: "Scope", value: "Calculated when a worker starts" },
      { key: "dispatch", label: "Dispatch", value: "Sequential guarded source refreshes" },
      { key: "evidence", label: "Evidence", value: "Source runs first; aggregate receipt on completion" },
    ],
  });
  assert.equal(
    commandExecutionPresentation(running)?.headline,
    "Bounded cadence pass in progress",
  );
  assert.equal(commandExecutionPresentation(command()), null);
});

test("a running single-source command describes source evidence separately", () => {
  const running = command({
    action: "refresh_source",
    source_key: "luma-sf",
    status: "running",
    completed_at: null,
  });
  const execution = commandExecutionPresentation(running);

  assert.equal(execution?.headline, "Single-source refresh in progress");
  assert.deepEqual(
    execution?.facts.map(({ label, value }) => [label, value]),
    [
      ["Scope", "One reviewed source"],
      ["Dispatch", "Guarded source refresh"],
      ["Evidence", "Source run plus candidate and catalog counts"],
    ],
  );
});

test("historical command failures are contained by the current fleet projection", () => {
  const value = command({
    result: {
      action: "refresh_due",
      due_sources: 1,
      attempted: 1,
      succeeded: 0,
      failed: 1,
    },
  });
  const sources = [
    {
      enabled: false,
      latest_run: { status: "failed", error: "page_cap_exceeded" },
    },
    {
      enabled: true,
      latest_run: { status: "succeeded", error: null },
    },
  ];

  assert.equal(commandReviewDisposition(value, sources), "contained");
  const outcome = commandOutcomePresentation(value, { containedHistorical: true });
  assert.equal(outcome.tone, "neutral");
  assert.equal(outcome.headline, "Historical issue contained");
  assert.match(outcome.detail, /retired or no longer has a current enabled failure/);
  assert.deepEqual(
    outcome.metrics.map(({ key, value: metricValue }) => [key, metricValue]),
    [["failed", "1"]],
  );
  assert.deepEqual(
    outcome.segments.map(({ key, value: segmentValue }) => [key, segmentValue]),
    [["failed", 1]],
  );
});

test("a historical receipt stays current while an enabled source remains failed", () => {
  const value = command({
    result: {
      action: "refresh_due",
      due_sources: 1,
      attempted: 1,
      failed: 1,
    },
  });

  assert.equal(commandReviewDisposition(value, [{
    enabled: true,
    latest_run: { status: "failed", error: "refresh_failed" },
  }]), "current");
  assert.equal(commandReviewDisposition(command(), []), "none");
});

test("a source receipt follows only its implicated source, not an unrelated fleet failure", () => {
  const value = command({
    action: "refresh_source",
    source_key: "retired-source",
    result: {
      action: "refresh_source",
      source_key: "retired-source",
      outcome: "failed",
    },
  });
  const sources = [
    {
      source_key: "retired-source",
      enabled: false,
      effective_status: "retired",
      retired_at: "2026-08-01T12:00:00Z",
      latest_run: { status: "failed", error: "refresh_failed" },
    },
    {
      source_key: "unrelated-source",
      enabled: true,
      effective_status: "active",
      retired_at: null,
      latest_run: { status: "failed", error: "refresh_failed" },
    },
  ];

  assert.equal(commandReviewDisposition(value, sources), "contained");
  assert.equal(
    commandOutcomePresentation(value, { containedHistorical: true }).headline,
    "Historical issue contained",
  );
});

test("a source receipt remains current only while that enabled source remains failed", () => {
  const value = command({
    action: "refresh_source",
    source_key: "current-source",
    result: {
      action: "refresh_source",
      source_key: "current-source",
      outcome: "failed",
    },
  });

  assert.equal(commandReviewDisposition(value, [{
    source_key: "current-source",
    enabled: true,
    effective_status: "active",
    retired_at: null,
    latest_run: { status: "failed", error: "refresh_failed" },
  }]), "current");
});

test("an unrelated enabled failure cannot revive a resolved source receipt", () => {
  const value = command({
    action: "refresh_source",
    source_key: "resolved-source",
    result: {
      action: "refresh_source",
      source_key: "resolved-source",
      outcome: "failed",
    },
  });

  assert.equal(commandReviewDisposition(value, [
    {
      source_key: "resolved-source",
      enabled: true,
      effective_status: "active",
      retired_at: null,
      latest_run: { status: "succeeded", error: null },
    },
    {
      source_key: "unrelated-source",
      enabled: true,
      effective_status: "active",
      retired_at: null,
      latest_run: { status: "failed", error: "refresh_failed" },
    },
  ]), "contained");
});

test("a failed worker command for a retired source is contained", () => {
  const value = command({
    action: "refresh_source",
    source_key: "retired-source",
    status: "failed",
    result: null,
    error_code: "source_unavailable",
  });
  const sources = [{
    source_key: "retired-source",
    enabled: false,
    effective_status: "retired",
    retired_at: "2026-08-01T12:00:00Z",
    latest_run: { status: "failed", error: "source_unavailable" },
  }];

  assert.equal(commandNeedsReview(value), false);
  assert.equal(commandReviewDisposition(value, sources), "contained");
  const outcome = commandOutcomePresentation(value, { containedHistorical: true });
  assert.equal(outcome.tone, "neutral");
  assert.equal(outcome.headline, "Historical issue contained");
});
