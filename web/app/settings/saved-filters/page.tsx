import type { Metadata } from "next";

import { SavedFiltersPanel } from "@/components/settings/saved-filters-panel";

export const metadata: Metadata = { title: "Saved filters" };

export default function SavedFiltersSettingsPage() {
  return <SavedFiltersPanel />;
}
