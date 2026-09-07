import type { Metadata } from "next";

import { ApiKeysPanel } from "@/components/settings/api-keys-panel";

export const metadata: Metadata = { title: "API keys" };

export default function ApiKeysSettingsPage() {
  return <ApiKeysPanel />;
}
