import type { ComponentType } from "react";
import { MuseIcon } from "./muse-icon";
import { MUSE_URL, museProviderUrl, museStatusLabel } from "@/lib/muse";
import { MUSE_PROVIDER, type RegistrationProviderId } from "@/lib/registration-providers";
import type { RegistrationStatus } from "@/lib/registrations";

/** Registration executors are separate from event publishers such as Luma and Meetup. */
export interface RegistrationProvider {
  key: RegistrationProviderId;
  label: string;
  Icon: ComponentType;
  openUrl: string;
  settingsUrl: string;
  statusLabels: Record<RegistrationStatus, string>;
  evidenceUrl: (value: string) => string | null;
}
export const museRegistrationProvider: RegistrationProvider = {
  key: MUSE_PROVIDER.id, label: MUSE_PROVIDER.label, Icon: MuseIcon, openUrl: MUSE_URL,
  settingsUrl: MUSE_PROVIDER.settingsHref,
  statusLabels: museStatusLabel, evidenceUrl: museProviderUrl,
};
