import assert from "node:assert/strict";
import test from "node:test";

import { accountDeletionUnavailable, signInFailurePath, signInHref, signInMessage, signInReason, signInReturnTo } from "../lib/sign-in.ts";

const config = { local_demo: false, auth_mode: "deployment_session", auth_provider: "google", auth_start_url: "/auth/login" };
const origin = "https://events.example.test";

test("provider and duplicate query values never become product messages", () => {
  for (const raw of ["access_token=private", "<script>private</script>", ["cancelled", "private"], {}, 42]) {
    assert.equal(signInReason(raw), "unknown");
    assert.deepEqual(signInMessage(signInReason(raw)), signInMessage("unknown"));
  }
  assert.equal(signInReason(undefined), null);
  assert.equal(signInReason(null), null);
});

test("cancelled, rejected, unavailable and revoked sessions have distinct terminal messages", () => {
  const reasons = ["cancelled", "not_authorized", "unavailable", "signed_out"];
  for (const reason of reasons) assert.equal(signInReason(reason), reason);
  assert.equal(new Set(reasons.map((reason) => signInMessage(reason).title)).size, reasons.length);
});

test("built-in Google and custom claim always start at the local BFF", () => {
  for (const auth_provider of ["google", "custom_claim"]) {
    assert.equal(signInHref({ ...config, auth_provider, auth_start_url: "https://untrusted.test" }, origin), "/auth/login");
  }
});

test("older configurations may use a same-origin start URL", () => {
  for (const url of ["/auth/login", `${origin}/auth/login`]) {
    assert.equal(signInHref({ ...config, auth_provider: undefined, auth_start_url: url }, origin), "/auth/login");
  }
});

test("sign-in rejects external, ambiguous and credential-bearing URLs", () => {
  for (const url of ["https://evil.test", "//evil.test", "/\\evil.test", "javascript:alert(1)", "data:text/html,private", "https://user@events.example.test/auth/login", "/auth/\nlogin", "/auth/ login", "x".repeat(2049), null]) {
    assert.equal(signInHref({ ...config, auth_provider: null, auth_start_url: url }, origin), null, String(url));
  }
});

test("local demo always goes to its existing onboarding route", () => {
  assert.equal(signInHref({ ...config, local_demo: true }, origin), "/");
});

test("identity errors never restart authentication or encode provider details", () => {
  for (const status of [401, 404]) assert.equal(signInFailurePath(status), "/sign-in");
  assert.equal(signInFailurePath(403), "/sign-in?reason=not_authorized");
  for (const status of [500, 503, null]) assert.equal(signInFailurePath(status), "/sign-in?reason=unavailable");
});

test("Google deletion is unavailable without affecting local demo or custom claim", () => {
  assert.equal(accountDeletionUnavailable(config), true);
  assert.equal(accountDeletionUnavailable({ ...config, auth_provider: "custom_claim" }), false);
  assert.equal(accountDeletionUnavailable({ ...config, auth_provider: null }), false);
  assert.equal(accountDeletionUnavailable({ ...config, auth_mode: "local_demo" }), false);
  assert.equal(accountDeletionUnavailable(null), false);
  assert.equal(accountDeletionUnavailable({ ...config, auth_provider: "identity_platform" }), false);
});

test("personal destinations survive sign-in without open redirects", () => {
  for (const path of ["/settings", "/settings/account", "/settings/saved-filters", "/settings/taste", "/?view=calendar"]) {
    assert.equal(signInReturnTo(path), path);
  }
  for (const path of ["https://evil.test", "//evil.test", "/\\evil.test", "/admin", "/auth/login", "/unknown"]) {
    assert.equal(signInReturnTo(path), "/");
  }
  assert.equal(signInFailurePath(401, "/settings/saved-filters"), "/sign-in?return_to=%2Fsettings%2Fsaved-filters");
  assert.equal(signInFailurePath(428, "/settings"), "/sign-in?reason=terms_updated&return_to=%2Fsettings");
  assert.equal(signInFailurePath(401, "https://evil.test"), "/sign-in");
  assert.equal(signInReason("terms_updated"), "terms_updated");
});
