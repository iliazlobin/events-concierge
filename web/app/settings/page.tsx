import type { Metadata } from "next";

import { ProfilePanel } from "@/components/settings/profile-panel";

export const metadata: Metadata = { title: "Profile" };

export default function ProfileSettingsPage() {
  return <ProfilePanel />;
}
