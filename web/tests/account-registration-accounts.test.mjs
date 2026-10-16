import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, setCsrfContract } from "../lib/api.ts";
import {
  EMPTY_REGISTRATION_ACCOUNT, getRegistrationAccount, normalizeRegistrationEmail,
  registrationAccountDraft, registrationAccountValues, saveRegistrationAccount, validateRegistrationEmail,
} from "../lib/registration-accounts.ts";

test("unset and cleared provider identities use the current browser without an owner-email fallback", () => {
  assert.deepEqual(registrationAccountDraft({ expected_email: null, provider_overrides: {} }), EMPTY_REGISTRATION_ACCOUNT);
  assert.deepEqual(registrationAccountValues({ defaultEmail: "  ", luma: " ", meetup: "", partiful: "" }), { expected_email: null, provider_overrides: {} });
  assert.deepEqual(registrationAccountValues({ defaultEmail: " Provider+events@EXAMPLE.TEST ", luma: "Luma@EXAMPLE.TEST", meetup: "", partiful: "" }), {
    expected_email: "Provider+events@example.test", provider_overrides: { luma: "Luma@example.test" },
  });
  assert.equal(normalizeRegistrationEmail("Provider+events@EXAMPLE.TEST"), "Provider+events@example.test");
});

test("Partiful overrides round-trip independently and clear to the default without phone metadata", () => {
  const values = { expected_email: "default@example.test", provider_overrides: { partiful: "Partiful+events@example.test" } };
  const draft = registrationAccountDraft(values);
  assert.equal(draft.partiful, "Partiful+events@example.test");
  assert.deepEqual(registrationAccountValues(draft), values);
  assert.deepEqual(registrationAccountValues({ ...draft, partiful: "  " }), {
    expected_email: "default@example.test", provider_overrides: {},
  });
  assert.deepEqual(Object.keys(draft).sort(), ["defaultEmail", "luma", "meetup", "partiful"]);
});

test("registration email accepts practical aliases and rejects ambiguous or unsupported identities", () => {
  for (const email of ["", "  ", " person+events@EXAMPLE.TEST ", "first.last@sub.example.test", "x@e.co"]) assert.equal(validateRegistrationEmail(email), null, email);
  for (const email of ["no-at", "person@example", "person@@example.test", "first..last@example.test", ".first@example.test",
    "last.@example.test", "person@-example.test", "person@example-.test", "person@exam_ple.test", "person@example.1a",
    "person@example.test\n", "person\t@example.test", "\u00a0person@example.test", "müsic@example.test", "\"person\"@example.test",
    "x".repeat(65) + "@example.test", "x@" + "a".repeat(64) + ".test", "x@" + ("a".repeat(63) + ".").repeat(4) + "test"]) assert.ok(validateRegistrationEmail(email), email);
});

test("registration account reads and saves use owner-scoped CSRF with only metadata and exact next revision", async (t) => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  t.after(() => { globalThis.fetch = originalFetch; globalThis.document = originalDocument; setCsrfContract(null, null); });
  globalThis.document = { cookie: "fixture_csrf=bound-token" };
  setCsrfContract("fixture_csrf", "X-Fixture-CSRF");
  const calls = [];
  const response = { executor: "muse", expected_email: "provider@example.test", provider_overrides: {}, revision: 2, updated_at: "2030-06-01T00:00:00Z" };
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); return Response.json(response); };
  const signal = new AbortController().signal;
  await getRegistrationAccount("muse", "owner", signal);
  assert.deepEqual(await saveRegistrationAccount("muse", { expected_email: response.expected_email, provider_overrides: {} }, 2, "owner"), response);
  assert.equal(calls.length, 2);
  for (const call of calls) { assert.equal(call.path, "/v1/me/registration-accounts/muse"); assert.equal(call.options.credentials, "same-origin"); assert.equal(call.options.headers.get("X-EC-Tenant-ID"), "owner"); }
  assert.equal(calls[0].options.signal, signal);
  assert.equal(calls[0].options.headers.get("X-Fixture-CSRF"), null);
  assert.equal(calls[1].options.method, "PUT");
  assert.equal(calls[1].options.headers.get("X-Fixture-CSRF"), "bound-token");
  assert.deepEqual(JSON.parse(calls[1].options.body), { expected_email: "provider@example.test", provider_overrides: {}, revision: 2 });
  assert.equal(calls[1].options.headers.get("Authorization"), null);
});

test("a stale account save exposes the conflict without retrying or swallowing a failed save", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ detail: "Registration account changed" }, { status: 409 }); };
  await assert.rejects(saveRegistrationAccount("muse", { expected_email: null, provider_overrides: {} }, 2, "owner"), error => error instanceof ApiError && error.status === 409);
  assert.equal(calls, 1);
});
