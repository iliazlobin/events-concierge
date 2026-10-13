import type { UiConfig } from "./types.ts";

/** Only implemented providers belong in this selector. */
export const REGISTRATION_PROVIDERS = [
  { id: "muse", label: "Muse", guideHref: "/docs/muse", enabled: (config: UiConfig | null) => config?.muse_enabled === true },
] as const;

export type RegistrationProviderId = typeof REGISTRATION_PROVIDERS[number]["id"];
export function availableRegistrationProviders(config: UiConfig | null) {
  return REGISTRATION_PROVIDERS.filter(provider => provider.enabled(config));
}
