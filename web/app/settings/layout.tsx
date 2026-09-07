import type { Metadata } from "next";

import { SessionProvider } from "@/components/session-provider";
import { SettingsShell } from "@/components/settings/settings-shell";

export const metadata: Metadata = {
  title: { default: "Settings", template: "%s · Events Concierge" },
};

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <SettingsShell>{children}</SettingsShell>
    </SessionProvider>
  );
}
