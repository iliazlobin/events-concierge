import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const SOURCE = readFileSync(
  new URL("../components/settings/security-panel.tsx", import.meta.url),
  "utf8",
);

/**
 * The security page is an unimplemented design preview. These assertions exist so it cannot quietly
 * become interactive: a mock that looks live but discards what it receives is worse than one that
 * obviously does nothing, and that gap is at its most harmful around credentials.
 */

test("the preview collects no input of any kind", () => {
  assert.equal(SOURCE.includes("<input"), false, "the preview must not render input fields");
  assert.equal(SOURCE.includes("<form"), false, "the preview must not render a form");
});

test("no password field can exist here while the page is a mock", () => {
  assert.equal(SOURCE.includes('type="password"'), false);
  // The product authenticates via OIDC and holds no password; a field here would collect a
  // credential it has no way to verify, store, or rotate.
  assert.equal(/autoComplete=["']?(new|current)-password/.test(SOURCE), false);
});

test("every control is inert", () => {
  const buttons = SOURCE.match(/<button\b/g) ?? [];
  const disabled = SOURCE.match(/\bdisabled\b/g) ?? [];
  assert.ok(buttons.length > 0, "the preview should still show the controls being proposed");
  assert.ok(
    disabled.length >= buttons.length,
    `every one of the ${buttons.length} buttons must be disabled, saw ${disabled.length}`,
  );
});

test("the page says plainly that it is not connected", () => {
  assert.match(SOURCE, /Design preview/);
  assert.match(SOURCE, /inert|not connected/i);
});

test("sample data is labelled as sample data", () => {
  assert.match(SOURCE, /Not real activity/i);
});

test("signing out everywhere is never gated behind proving yourself again", () => {
  // De-escalation must stay reachable: the person who most needs this button may be sharing their
  // account with an attacker right now.
  assert.match(SOURCE, /never ask you to prove yourself again/i);
});
