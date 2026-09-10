/** A missing or invalid value cannot establish a source's collection contract. */
export function collectionHorizonDays(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 90
    ? value : null;
}

export function collectionHorizonLabel(value: unknown): string {
  const days = collectionHorizonDays(value);
  return days === null ? "Not recorded" : `Upcoming ${days} ${days === 1 ? "day" : "days"}`;
}
