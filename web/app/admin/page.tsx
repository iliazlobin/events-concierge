import type { Metadata } from "next";

import { AdminConsole } from "@/components/admin/admin-console";

export const metadata: Metadata = {
  title: "Ingestion Operations · Events Concierge",
  description: "Local crawler health, run history, diagnostics, and durable controls.",
};

export default function AdminPage() {
  return <AdminConsole />;
}
