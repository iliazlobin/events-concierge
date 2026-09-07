import type { AdminSource } from "@/lib/admin-types";

type SourceLifecycle = Pick<
  AdminSource,
  | "effective_status"
  | "retired_at"
  | "retired_reason"
  | "superseded_by_source_key"
>;

export function adminSourceIsRetired(source: SourceLifecycle): boolean {
  return source.retired_at != null || source.effective_status === "retired";
}

export function adminSourceLifecycleLabel(source: SourceLifecycle): string {
  if (!adminSourceIsRetired(source)) return "";
  return source.superseded_by_source_key ? "Superseded" : "Retired";
}

export function adminSourceRetirementDescription(source: SourceLifecycle): string {
  if (source.superseded_by_source_key) {
    return "Collection ended because another reviewed source is now authoritative. Historical runs and catalog evidence remain available.";
  }
  if (source.retired_reason) {
    return source.retired_reason
      .replaceAll("_", " ")
      .replace(/^./, (character) => character.toUpperCase());
  }
  return "Collection is permanently retired. Historical runs and catalog evidence remain available.";
}
