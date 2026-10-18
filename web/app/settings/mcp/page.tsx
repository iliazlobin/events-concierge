import type { Metadata } from "next";
import { McpPanel } from "@/components/settings/mcp-panel";
export const metadata: Metadata = { title: "MCP (events discovery)" };
export default function McpSettingsPage() { return <McpPanel />; }
