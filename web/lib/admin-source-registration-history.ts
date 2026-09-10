import type { AdminSourceRegistrationHistory } from "./admin-types.ts";

export type SourceHistoryDays = 7 | 30 | 90;

export function sourceHistoryDays(value: string | null): SourceHistoryDays {
  return value === "7" ? 7 : value === "30" ? 30 : 90;
}

export function sourceRegistrationPlot(history: AdminSourceRegistrationHistory) {
  const values = [
    { at: history.window_start, count: history.baseline_sources, added: 0 },
    ...history.items.map(item => ({ at: item.bucket_end, count: item.registered_sources, added: item.added_sources })),
  ];
  const maximum = Math.max(1, ...values.map(value => value.count));
  const start = Date.parse(history.window_start);
  const span = Math.max(1, Date.parse(history.generated_at) - start);
  const points = values.map((value, index) => ({ ...value, index,
    x: (Date.parse(value.at) - start) / span * 1000,
    y: 190 - value.count / maximum * 180,
  }));
  // Daily cumulative counts: keep quiet intervals visible and avoid suggesting
  // intermediate registrations that were not measured by this projection.
  const path = points.map((point, index) => index === 0
    ? `M ${point.x} ${point.y}` : `H ${point.x} V ${point.y}`).join(" ");
  return { points, path, maximum };
}

export function registrationTimestamp(value: string): string {
  return new Date(value).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
