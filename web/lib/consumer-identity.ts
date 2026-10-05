import { getApp, initializeApp } from "firebase/app";
import {
  getAuth, GoogleAuthProvider, inMemoryPersistence, OAuthProvider,
  setPersistence, signInWithPopup, signOut,
} from "firebase/auth";

import type { ConsumerIdentityConfig } from "./types.ts";

export type ConsumerProvider = "google.com" | "apple.com";

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
    async signIn(provider: ConsumerProvider): Promise<string> {
      if (!config.providers.includes(provider)) throw new Error("This sign-in option is unavailable.");
      const oauth = provider === "google.com" ? new GoogleAuthProvider() : new OAuthProvider("apple.com");
      if (provider === "google.com") oauth.setCustomParameters({ prompt: "select_account" });
      else { oauth.addScope("email"); oauth.addScope("name"); }
      const result = await signInWithPopup(auth, oauth);
      return result.user.getIdToken();
    },
    clear() { return signOut(auth); },
  };
}

export function identityErrorMessage(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? error.code : null;
  if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") return "Sign-in cancelled. Try again when you’re ready.";
  if (code === "auth/popup-blocked") return "Allow this site’s sign-in popup, then try again.";
  return "We couldn’t finish signing you in. Please try again.";
}
