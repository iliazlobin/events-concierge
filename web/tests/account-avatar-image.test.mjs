import assert from "node:assert/strict";
import test from "node:test";

import { AVATAR_EDGE, AVATAR_MAX_BYTES, isAcceptedAvatarType } from "../lib/avatar-image.ts";

/**
 * The proxy and the server body guard both cap a request at 64 KiB. If someone raises the avatar
 * ceiling past that, uploads start failing at the transport before reaching any image code, with an
 * error that points nowhere useful. Fail here instead.
 */
test("the avatar bound stays under the request body cap", () => {
  assert.ok(AVATAR_MAX_BYTES < 64 * 1024, "avatar bound must stay under the 64 KiB body cap");
});

test("the client crop target matches what the server re-encodes to", () => {
  assert.equal(AVATAR_EDGE, 256);
});

test("only decodable raster types are offered", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp"]) {
    assert.ok(isAcceptedAvatarType(type), `${type} should be accepted`);
  }
  assert.ok(isAcceptedAvatarType("IMAGE/PNG"), "type matching is case-insensitive");
});

test("SVG is never an accepted avatar type", () => {
  // Served same-origin under img-src 'self', a stored SVG is XSS against the origin that holds
  // the readable CSRF cookie. It must not be offered, and the server refuses it regardless.
  assert.equal(isAcceptedAvatarType("image/svg+xml"), false);
  assert.equal(isAcceptedAvatarType("image/gif"), false);
  assert.equal(isAcceptedAvatarType("text/html"), false);
});
