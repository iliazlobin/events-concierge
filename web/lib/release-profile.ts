import type { ConsumerHistorySnapshot } from "./consumer-history.ts";
import type { UiConfig, ViewName } from "./types.ts";

export type ReleaseProfile = "full" | "discovery";

/** Missing config is closed; an older successful config retains its full-profile contract. */
export function releaseProfile(config: UiConfig | null): ReleaseProfile {
  return config && (config.release_profile === undefined || config.release_profile === "full")
    ? "full"
    : "discovery";
}

export function releaseViewAllowed(view: ViewName, profile: ReleaseProfile): boolean {
  return profile === "full" || view !== "chat";
}

export function releaseHome(profile: ReleaseProfile): ViewName {
  return profile === "full" ? "chat" : "events";
}

/** Browser state and URL parameters cannot enable a server-disabled workspace. */
export function releaseHistorySnapshot(
  snapshot: ConsumerHistorySnapshot,
  profile: ReleaseProfile,
): ConsumerHistorySnapshot {
  if (profile === "full") return snapshot;
  const view = releaseViewAllowed(snapshot.view, profile) ? snapshot.view : "events";
  return {
    ...snapshot,
    view,
    selectedEntityId: view === "entities" ? snapshot.selectedEntityId : null,
  };
}

export function releaseSettingsAllowed(href: string, profile: ReleaseProfile): boolean {
  return profile === "full" || !["/settings/activity", "/settings/security", "/settings/api-keys"].includes(href);
}
