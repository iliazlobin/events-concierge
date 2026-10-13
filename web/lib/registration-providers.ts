import type { UiConfig } from "./types.ts";

export const MUSE_PROVIDER = {
  id: "muse", label: "Muse", guideHref: "/docs/muse", settingsHref: "/settings/registrations",
  enabled: (config: UiConfig | null) => config?.muse_enabled === true,
} as const;

/** Only implemented providers belong in this selector. */
export const REGISTRATION_PROVIDERS = [MUSE_PROVIDER] as const;

export type RegistrationProviderId = typeof REGISTRATION_PROVIDERS[number]["id"];
export function availableRegistrationProviders(config: UiConfig | null) {
  return REGISTRATION_PROVIDERS.filter(provider => provider.enabled(config));
}
