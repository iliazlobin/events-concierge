import { getApp, initializeApp } from "firebase/app";
import {
  AuthErrorCodes, getAuth, GoogleAuthProvider, inMemoryPersistence, OAuthProvider,
  setPersistence, signInWithPopup, signOut,
} from "firebase/auth";

import type { UserCredential } from "firebase/auth";

import type { ConsumerIdentityConfig } from "./types.ts";

export type ConsumerProvider = "google.com" | "apple.com";

export function consumerOAuthProvider(provider: ConsumerProvider) {
  const oauth = provider === "google.com" ? new GoogleAuthProvider() : new OAuthProvider("apple.com");
  // The API requires a verified email for account creation and reauthentication.
  oauth.addScope("email");
  if (provider === "google.com") oauth.setCustomParameters({ prompt: "select_account" });
  else oauth.addScope("name");
  return oauth;
}

export type ConsumerIdentityTokens = { idToken: string; googleIdToken?: string };

/** Return only the signed proof needed by our API, never the credential/error object. */
export async function consumerIdentityTokens(
  result: UserCredential, provider: ConsumerProvider,
): Promise<ConsumerIdentityTokens> {
  const googleIdToken = provider === "google.com"
    ? GoogleAuthProvider.credentialFromResult(result)?.idToken : undefined;
  if (provider === "google.com" && (typeof googleIdToken !== "string" || !googleIdToken)) {
    throw new Error("Google identity proof is unavailable.");
  }
  const idToken = await result.user.getIdToken();
  return { idToken, ...(googleIdToken ? { googleIdToken } : {}) };
}

/** The SDK exists only on the sign-in page. Refresh/identity tokens never enter browser storage. */
export async function prepareConsumerIdentity(config: ConsumerIdentityConfig) {
  const name = `events-concierge-${config.project_id}`;
  let app;
  try { app = getApp(name); }
  catch {
    app = initializeApp({
      projectId: config.project_id, apiKey: config.api_key, authDomain: config.auth_domain,
    }, name);
  }
  if (app.options.projectId !== config.project_id || app.options.apiKey !== config.api_key
      || app.options.authDomain !== config.auth_domain) {
    throw new Error("Sign-in configuration changed. Reload this page.");
  }
  const auth = getAuth(app);
  await setPersistence(auth, inMemoryPersistence);
  await signOut(auth);
  return {
    async signIn(provider: ConsumerProvider): Promise<ConsumerIdentityTokens> {
      if (!config.providers.includes(provider)) throw new Error("This sign-in option is unavailable.");
      const oauth = consumerOAuthProvider(provider);
      const result = await signInWithPopup(auth, oauth);
      return consumerIdentityTokens(result, provider);
    },
    clear() { return signOut(auth); },
  };
}

/** Only SDK-defined codes and HTTP status; never log a thrown object or its message. */
export function identityFailureDiagnostic(
  error: unknown, stage: "provider" | "session" | "clear",
) {
  const code = typeof error === "object" && error !== null && "code" in error ? error.code : null;
  const status = typeof error === "object" && error !== null && "status" in error ? error.status : null;
  return {
    stage,
    code: typeof code === "string" && Object.values(AuthErrorCodes).some(value => value === code)
      ? code : "unknown",
    status: typeof status === "number" && Number.isInteger(status) && status >= 400 && status <= 599
      ? status : null,
  };
}

export function identityErrorMessage(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? error.code : null;
  if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") return "Sign-in cancelled. Try again when you’re ready.";
  if (code === "auth/popup-blocked") return "Allow this site’s sign-in popup, then try again.";
  return "We couldn’t finish signing you in. Please try again.";
}
