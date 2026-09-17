import type { Metadata } from "next";

import { SignIn } from "@/components/sign-in";
import { signInReason } from "@/lib/sign-in";

export const metadata: Metadata = {
  title: "Sign in · Events Concierge",
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

/** Public landing, deliberately outside both the session tree and the /auth API proxy. */
export default async function SignInPage({ searchParams }: {
  searchParams: Promise<{ reason?: string | string[] }>;
}) {
  const { reason } = await searchParams;
  return <SignIn reason={signInReason(reason)} />;
}
