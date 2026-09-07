import type { Metadata } from "next";

import { TastePanel } from "@/components/settings/taste-panel";

export const metadata: Metadata = { title: "Interests" };

export default function TasteSettingsPage() {
  return <TastePanel />;
}
