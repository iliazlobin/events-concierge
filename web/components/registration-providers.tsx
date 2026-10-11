import type { ComponentType } from "react";
import { MuseIcon } from "./muse-icon";
import { MUSE_URL, museProviderUrl, museStatusLabel } from "@/lib/muse";
import type { RegistrationStatus } from "@/lib/registrations";

/** Registration executors are separate from event publishers such as Luma and Meetup. */
export interface RegistrationProvider {
  key: string;
  label: string;
  Icon: ComponentType;
  openUrl: string;
  settingsUrl: string;
  statusLabels: Record<RegistrationStatus, string>;
  evidenceUrl: (value: string) => string | null;
}
export const museRegistrationProvider: RegistrationProvider = {
  key: "muse", label: "Muse", Icon: MuseIcon, openUrl: MUSE_URL, settingsUrl: "/settings/muse",
  statusLabels: museStatusLabel, evidenceUrl: museProviderUrl,
};
