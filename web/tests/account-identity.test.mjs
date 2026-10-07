import assert from "node:assert/strict";
import test from "node:test";

import { consumerIdentityTokens, consumerOAuthProvider, identityFailureDiagnostic } from "../lib/consumer-identity.ts";

test("Google supplies the email identity required by the account API", () => {
  const provider = consumerOAuthProvider("google.com");
  assert.equal(provider.providerId, "google.com");
  assert.deepEqual(new Set(provider.getScopes()), new Set(["profile", "email"]));
  assert.deepEqual(provider.getCustomParameters(), { prompt: "select_account" });
});

test("Apple retains email and name without Google parameters", () => {
  const provider = consumerOAuthProvider("apple.com");
  assert.equal(provider.providerId, "apple.com");
  assert.deepEqual(new Set(provider.getScopes()), new Set(["email", "name"]));
  assert.deepEqual(provider.getCustomParameters(), {});
});

test("diagnostics distinguish provider and session failures without credentials", () => {
  assert.deepEqual(identityFailureDiagnostic({ code: "auth/network-request-failed", message: "private" }, "provider"),
    { stage: "provider", code: "auth/network-request-failed", status: null });
  assert.deepEqual(identityFailureDiagnostic({ status: 401, message: "private", id_token: "private" }, "session"),
    { stage: "session", code: "unknown", status: 401 });
  for (const error of [null, "private", { code: "auth/private-secret", status: "private" },
    { code: "private", status: 3.14, customData: { email: "private", credential: "private" } },
    { status: 200 }, { status: 600 }]) {
    assert.deepEqual(identityFailureDiagnostic(error, "clear"),
      { stage: "clear", code: "unknown", status: null });
  }
});


function credentialFixture(tokenResponse) {
  return { user: { getIdToken: async () => "synthetic-firebase-id-token" },
    providerId: "google.com", operationType: "signIn", _tokenResponse: tokenResponse };
}

test("Google exchange carries the SDK's two signed ID tokens only", async () => {
  const result = credentialFixture({ oauthIdToken: "synthetic-google-id-token",
    oauthAccessToken: "private-access-token", email: "unsigned-profile@example.test" });
  assert.deepEqual(await consumerIdentityTokens(result, "google.com"), {
    idToken: "synthetic-firebase-id-token", googleIdToken: "synthetic-google-id-token",
  });
});

test("missing Google ID token cannot fall back to access token or profile email", async () => {
  for (const response of [undefined, {}, { oauthAccessToken: "private-access-token" },
    { email: "unsigned-profile@example.test", emailVerified: true }, { oauthIdToken: true }]) {
    await assert.rejects(consumerIdentityTokens(credentialFixture(response), "google.com"),
      { message: "Google identity proof is unavailable." });
  }
});

test("Apple sends only the Firebase proof", async () => {
  assert.deepEqual(await consumerIdentityTokens(credentialFixture({ oauthIdToken: "unused" }), "apple.com"),
    { idToken: "synthetic-firebase-id-token" });
});
