import type { Metadata } from "next";

import { AppLanding } from "@/components/app-landing";

export const metadata: Metadata = { title: "Events Concierge" };

/**
 * The post-login landing.
 *
 * Sign-in returns to `/app` by default and re-authentication returns to `/app` plus a fragment, but
 * this route did not exist -- so every completed sign-in landed on a 404. It resolves the fragment
 * against a fixed allowlist and forwards.
 */
export default function AppLandingPage() {
  return <AppLanding />;
}
