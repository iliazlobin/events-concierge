import type { Metadata } from "next";
import { RegistrationPanel } from "@/components/settings/registration-panel";

export const metadata: Metadata = { title: "Registrations" };
export default function RegistrationSettingsPage() { return <RegistrationPanel />; }
