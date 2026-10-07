import type { Metadata } from "next";
import { MusePanel } from "@/components/settings/muse-panel";

export const metadata: Metadata = { title: "Muse signups" };
export default function MuseSettingsPage() { return <MusePanel />; }
