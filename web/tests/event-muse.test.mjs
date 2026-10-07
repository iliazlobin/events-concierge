import assert from "node:assert/strict";
import test from "node:test";
import { museEligibility, museInstruction, museProviderUrl, museStatusLabel, saveMuseSelection, takeMuseSelection } from "../lib/muse.ts";

const id = "22222222-2222-4222-8222-222222222222";
const now = Date.parse("2026-10-07T12:00:00Z");
const event = {
  canonical_event_id: id, start_at: "2030-06-14T19:30:00-07:00", price_status: "free",
  event_status: "scheduled", registration_status: "open", registration_urls: ["https://lu.ma/event"],
  sources: [],
};

test("only upcoming free Luma or Meetup occurrences can be selected", () => {
  assert.equal(museEligibility(event, now), null);
  for (const changes of [
    { price_status: "paid" }, { price_status: "unknown" }, { event_status: "cancelled" },
    { start_at: "invalid" }, { start_at: "2020-01-01T00:00:00Z" }, { registration_status: "sold_out" },
    { registration_urls: ["https://unreviewed.test/event"] },
  ]) assert.ok(museEligibility({ ...event, ...changes }, now));
});

test("provider URLs never admit credential links or another origin", () => {
  assert.equal(museProviderUrl("https://www.meetup.com/group/events/123/"), "https://www.meetup.com/group/events/123/");
  for (const url of ["javascript:alert(1)", "https://lu.ma.evil.test/event", "https://lu.ma/",
    "https://user:secret@lu.ma/event", "https://lu.ma:444/event", "https://lu.ma/event#token",
    "https://lu.ma/event\n", "https://lu.ma/\\evil"]) assert.equal(museProviderUrl(url), null);
});

test("sign-in continuation stores public IDs only, expires and is consumed once", () => {
  const entries = new Map();
  const storage = {
    setItem: (key, value) => entries.set(key, value),
    getItem: key => entries.get(key) ?? null, removeItem: key => entries.delete(key),
  };
  saveMuseSelection(storage, [id, id, "not-an-id"], now);
  assert.deepEqual(JSON.parse([...entries.values()][0]), { eventIds: [id], savedAt: now });
  assert.deepEqual(takeMuseSelection(storage, now + 1), [id]);
  assert.deepEqual(takeMuseSelection(storage, now + 2), []);
  saveMuseSelection(storage, [id], now);
  assert.deepEqual(takeMuseSelection(storage, now + 31 * 60 * 1000), []);
});

test("handoff preserves approvals and distinguishes reported registration", () => {
  const instruction = museInstruction(id);
  assert.ok(instruction.includes(id) && instruction.includes("Claim each event"));
  assert.ok(instruction.includes("approval") && instruction.includes("uncertain"));
  assert.equal(museStatusLabel.registered, "Registered · reported by Muse");
  assert.notEqual(museStatusLabel.awaiting_approval, museStatusLabel.registered);
});
