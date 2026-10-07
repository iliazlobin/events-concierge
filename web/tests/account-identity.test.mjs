import assert from "node:assert/strict";
import test from "node:test";

import { consumerOAuthProvider, identityFailureDiagnostic } from "../lib/consumer-identity.ts";

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
