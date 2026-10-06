import type { UiConfig } from "./types.ts";

export type SignInReason = "cancelled" | "not_authorized" | "unavailable" | "signed_out" | "terms_updated" | "unknown" | null;

/** Only product-owned messages reach the page; never render provider query parameters. */
export function signInReason(value: unknown): SignInReason {
  if (value === undefined || value === null) return null;
  if (value === "cancelled" || value === "not_authorized" || value === "unavailable" || value === "signed_out" || value === "terms_updated") return value;
  return "unknown";
}

export function signInMessage(reason: SignInReason): { title: string; description: string } {
  switch (reason) {
    case "cancelled":
      return { title: "Sign-in cancelled", description: "You stopped signing in. Continue when you’re ready." };
    case "not_authorized":
      return { title: "This account can’t sign in", description: "Please try signing in again with a verified Google or Apple account." };
    case "unavailable":
      return { title: "Sign-in is temporarily unavailable", description: "We couldn’t finish signing you in. Please try again in a moment." };
    case "signed_out":
      return { title: "You’re signed out", description: "You can sign in again when you’re ready." };
    case "terms_updated":
      return { title: "Review the updated terms", description: "Accept the current Terms of Service and acknowledge the Privacy Policy to use your account." };
    case "unknown":
      return { title: "Sign-in wasn’t completed", description: "Please try signing in again." };
    default:
      return { title: "Sign in", description: "Browse events without an account. Sign in to save filters and preferences." };
  }
}

/** Authentication starts only through an explicit, same-origin navigation. */
export function signInHref(config: UiConfig, origin: string): string | null {
  if (config.local_demo) return "/";
  if (config.auth_provider === "identity_platform") return "/sign-in";
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

export function signInFailurePath(status: number | null, returnTo?: string): string {
  const params = new URLSearchParams();
  if (status === 403) params.set("reason", "not_authorized");
  else if (status === 428) params.set("reason", "terms_updated");
  else if (status !== 401 && status !== 404) params.set("reason", "unavailable");
  const destination = signInReturnTo(returnTo);
  if (destination !== "/") params.set("return_to", destination);
  const query = params.toString();
  return `/sign-in${query ? `?${query}` : ""}`;
}

export function accountDeletionUnavailable(config: UiConfig | null): boolean {
  return config?.auth_mode === "deployment_session" && config.auth_provider === "google";
}

/** A local path only; never forward a caller-selected origin to an identity callback. */
export function signInReturnTo(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048 || !value.startsWith("/") || value.startsWith("//") || /[\\\s\u0000-\u001f\u007f]/.test(value)) return "/";
  try {
    const url = new URL(value, "https://events.example.test");
    const allowed = ["/", "/app", "/settings", "/settings/account", "/settings/activity", "/settings/api-keys", "/settings/saved-filters", "/settings/security", "/settings/taste"];
    return url.origin === "https://events.example.test" && allowed.includes(url.pathname)
      ? `${url.pathname}${url.search}${url.hash}` : "/";
  } catch { return "/"; }
}
