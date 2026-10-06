import type { Metadata } from "next";

import { SignIn } from "@/components/sign-in";
import { signInReason, signInReturnTo } from "@/lib/sign-in";

export const metadata: Metadata = {
  title: "Sign in · Events Concierge",
  robots: { index: false, follow: false },
  referrer: "strict-origin-when-cross-origin",
};

/** Public landing, deliberately outside both the session tree and the /auth API proxy. */
export default async function SignInPage({ searchParams }: {
  searchParams: Promise<{ reason?: string | string[]; return_to?: string; reauth?: string; state?: string }>;
}) {
  const { reason, return_to, reauth, state } = await searchParams;
  return <SignIn reason={signInReason(reason)} returnTo={signInReturnTo(return_to)} reauthenticationState={reauth === "1" && /^[A-Za-z0-9_-]{43}$/.test(state ?? "") ? state : undefined} />;
}
