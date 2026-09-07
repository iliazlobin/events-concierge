import assert from "node:assert/strict";
import test from "node:test";

import { eventImageUrl } from "../lib/event-image.ts";

test("event previews accept only absolute HTTP image URLs", () => {
  assert.equal(
    eventImageUrl({ image_url: " https://images.example.test/event.jpg " }),
    "https://images.example.test/event.jpg",
  );
  assert.equal(
    eventImageUrl({ image_url: "http://127.0.0.1:8000/event.png" }),
    "http://127.0.0.1:8000/event.png",
  );
  assert.equal(eventImageUrl({ image_url: "javascript:alert(1)" }), null);
  assert.equal(eventImageUrl({ image_url: "/relative-event.jpg" }), null);
  assert.equal(eventImageUrl({ image_url: null }), null);
});
