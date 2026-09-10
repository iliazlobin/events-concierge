import type { Metadata } from "next";

import { AdminConsole } from "@/components/admin/admin-console";

export const metadata: Metadata = {
  title: "System Operations · Events Concierge",
  description: "Monitor operations, manage event sources, and inspect catalog records and execution history.",
};

export default function AdminPage() {
  return <AdminConsole />;
}
