import type { UiConfig } from "./types.ts";

export type SignInReason = "cancelled" | "not_authorized" | "unavailable" | "signed_out" | "unknown" | null;

/** Only product-owned messages reach the page; never render provider query parameters. */
export function signInReason(value: unknown): SignInReason {
  if (value === undefined || value === null) return null;
  if (value === "cancelled" || value === "not_authorized" || value === "unavailable" || value === "signed_out") return value;
  return "unknown";
}

export function signInMessage(reason: SignInReason): { title: string; description: string } {
  switch (reason) {
    case "cancelled":
      return { title: "Sign-in cancelled", description: "You stopped signing in. Continue when you’re ready." };
    case "not_authorized":
      return { title: "This account can’t sign in", description: "Try an account that has access to Events Concierge, or contact the person who invited you." };
    case "unavailable":
      return { title: "Sign-in is temporarily unavailable", description: "We couldn’t finish signing you in. Please try again in a moment." };
    case "signed_out":
      return { title: "You’re signed out", description: "You can sign in again when you’re ready." };
    case "unknown":
      return { title: "Sign-in wasn’t completed", description: "Please try signing in again." };
    default:
      return { title: "Find something worth going to.", description: "Sign in for a personal view of events around you." };
  }
}

/** Authentication starts only through an explicit, same-origin navigation. */
export function signInHref(config: UiConfig, origin: string): string | null {
  if (config.local_demo) return "/";
  if (config.auth_provider === "google" || config.auth_provider === "custom_claim") return "/auth/login";
  const candidate = config.auth_start_url;
  if (!candidate || candidate.length > 2048 || /[\\\s\u0000-\u001f\u007f]/.test(candidate) || candidate.startsWith("//")) return null;
  try {
    const url = new URL(candidate, origin);
    if (url.origin !== origin || url.username || url.password || !["http:", "https:"].includes(url.protocol)) return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export function signInFailurePath(status: number | null): string {
  if (status === 401 || status === 404) return "/sign-in";
  if (status === 403) return "/sign-in?reason=not_authorized";
  return "/sign-in?reason=unavailable";
}

export function accountDeletionUnavailable(config: UiConfig | null): boolean {
  return config?.auth_mode === "deployment_session" && config.auth_provider === "google";
}
