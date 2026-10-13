import type { Metadata } from "next";

import { AccountPanel } from "@/components/settings/account-panel";

export const metadata: Metadata = { title: "Account" };

export default function AccountSettingsPage() {
  return <AccountPanel />;
}
