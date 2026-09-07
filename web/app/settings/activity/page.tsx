import type { Metadata } from "next";

import { ActivityPanel } from "@/components/settings/activity-panel";

export const metadata: Metadata = { title: "Activity" };

export default function ActivitySettingsPage() {
  return <ActivityPanel />;
}
