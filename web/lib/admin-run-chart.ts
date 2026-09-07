import type { AdminRun } from "@/lib/admin-types";
import { isPacerDeferredRun } from "./admin-presentation.ts";

export type OverviewRunWindow = 24 | 168 | 720;

export interface RunBucket {
  key: string;
  label: string;
  dateLabel: string;
  succeeded: number;
  failed: number;
  resolved: number;
  running: number;
  paused: number;
  candidates: number;
  canonical: number;
  durationTotalMs: number;
  durationSamples: number;
  slowestDurationMs: number;
  sourceKeys: string[];
  failedSourceKeys: string[];
}

export const RUN_WINDOW_CONFIG: Record<OverviewRunWindow, {
  bucketCount: number;
  bucketHours: number;
  eyebrow: string;
  intervalLabel: string;
}> = {
  24: {
    bucketCount: 24,
    bucketHours: 1,
    eyebrow: "24-hour operating window",
    intervalLabel: "hour",
  },
  168: {
    bucketCount: 28,
    bucketHours: 6,
    eyebrow: "Seven-day operating window",
    intervalLabel: "6-hour interval",
  },
  720: {
    bucketCount: 30,
    bucketHours: 24,
    eyebrow: "30-day operating window",
    intervalLabel: "day",
  },
};

export function buildRunBuckets(
  runs: AdminRun[],
  generatedAt: string,
  windowHours: OverviewRunWindow,
): RunBucket[] {
  const generated = new Date(generatedAt);
  if (Number.isNaN(generated.getTime())) return [];
  const config = RUN_WINDOW_CONFIG[windowHours];
  const bucketMilliseconds = config.bucketHours * 3_600_000;
  const windowStart = generated.getTime() - windowHours * 3_600_000;
  const hourFormat = new Intl.DateTimeFormat("en-US", { hour: "numeric" });
  const shortDateFormat = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
  });
  const dateTimeFormat = new Intl.DateTimeFormat("en-US", {
    weekday: "short",
    hour: "numeric",
  });
  const shortDateTimeFormat = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
  });
  const buckets: RunBucket[] = Array.from({ length: config.bucketCount }, (_, index) => {
    const startsAt = new Date(windowStart + index * bucketMilliseconds);
    const endsAt = new Date(startsAt.getTime() + bucketMilliseconds - 1);
    const label = windowHours === 24
      ? hourFormat.format(startsAt)
      : windowHours === 168
        ? hourFormat.format(startsAt)
        : shortDateFormat.format(startsAt);
    const dateLabel = windowHours === 24
      ? dateTimeFormat.format(startsAt)
      : windowHours === 720
        ? `${shortDateFormat.format(startsAt)}–${shortDateFormat.format(endsAt)}`
        : shortDateTimeFormat.format(startsAt);
    return {
      key: startsAt.toISOString(),
      label,
      dateLabel,
      succeeded: 0,
      failed: 0,
      resolved: 0,
      running: 0,
      paused: 0,
      candidates: 0,
      canonical: 0,
      durationTotalMs: 0,
      durationSamples: 0,
      slowestDurationMs: 0,
      sourceKeys: [],
      failedSourceKeys: [],
    };
  });
  for (const run of runs) {
    if (!run.started_at) continue;
    const startedAt = new Date(run.started_at).getTime();
    if (
      Number.isNaN(startedAt)
      || startedAt < windowStart
      || startedAt > generated.getTime()
    ) {
      continue;
    }
    const bucketIndex = Math.min(
      config.bucketCount - 1,
      Math.floor((startedAt - windowStart) / bucketMilliseconds),
    );
    const bucket = buckets[bucketIndex];
    if (run.status === "succeeded") bucket.succeeded += 1;
    else if (isPacerDeferredRun(run)) bucket.paused += 1;
    else if (run.status === "failed") {
      if (run.resolved_by_newer_success) bucket.resolved += 1;
      else bucket.failed += 1;
      if (!bucket.failedSourceKeys.includes(run.source_key)) {
        bucket.failedSourceKeys.push(run.source_key);
      }
    } else if (run.status === "running") bucket.running += 1;
    else bucket.paused += 1;
    if (!bucket.sourceKeys.includes(run.source_key)) {
      bucket.sourceKeys.push(run.source_key);
    }
    if (!isPacerDeferredRun(run) && run.duration_ms !== null) {
      bucket.durationTotalMs += run.duration_ms;
      bucket.durationSamples += 1;
      bucket.slowestDurationMs = Math.max(bucket.slowestDurationMs, run.duration_ms);
    }
    if (
      run.status === "succeeded"
      && run.candidate_count !== null
      && run.canonical_count !== null
    ) {
      bucket.candidates += run.candidate_count;
      bucket.canonical += run.canonical_count;
    }
  }
  return buckets;
}

export function bucketTotal(bucket: RunBucket): number {
  return bucket.succeeded
    + bucket.failed
    + bucket.resolved
    + bucket.running
    + bucket.paused;
}

export function bucketFailureTotal(bucket: RunBucket): number {
  return bucket.failed + bucket.resolved;
}

export function bucketSuccessRate(bucket: RunBucket): number | null {
  const completed = bucket.succeeded + bucketFailureTotal(bucket);
  return completed ? bucket.succeeded / completed : null;
}

export function bucketYieldRate(bucket: RunBucket): number | null {
  return bucket.candidates ? bucket.canonical / bucket.candidates : null;
}

export function bucketAverageDuration(bucket: RunBucket): number | null {
  return bucket.durationSamples
    ? Math.round(bucket.durationTotalMs / bucket.durationSamples)
    : null;
}
