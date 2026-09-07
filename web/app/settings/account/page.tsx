import type { Metadata } from "next";

import { AccountPanel } from "@/components/settings/account-panel";

export const metadata: Metadata = { title: "Account and data" };

export default function AccountSettingsPage() {
  return <AccountPanel />;
}
