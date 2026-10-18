"use client";
import Link from "next/link";
import { useSession } from "@/components/session-provider";
import { availableRegistrationProviders } from "@/lib/registration-providers";
import { MuseConnectionPanel } from "./muse-panel";
export function McpPanel() {
  const { config } = useSession();
  return <section className="settings-panel">
    <header className="settings-panel__head"><h1>MCP discovery</h1><Link href="/docs/mcp" className="settings-guide-link">Setup guide <span aria-hidden="true">→</span></Link></header>
    {availableRegistrationProviders(config).length ? <MuseConnectionPanel purpose="discovery" /> : <p className="settings-hint">Not enabled.</p>}
  </section>;
}
