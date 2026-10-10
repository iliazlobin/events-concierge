import assert from "node:assert/strict";
import test from "node:test";
import {
  getMuseRegistrations, markMuseRegistrationsSeen, mergeMuseRegistrations, museEligibility, museInstruction,
  museProviderUrl, museRegistrationUpdates, museStatusLabel, museUnreadCount, queueMuseRegistration,
  saveMuseIntent, takeMuseIntent,
} from "../lib/muse.ts";

const id = "22222222-2222-4222-8222-222222222222";
const now = Date.parse("2026-10-07T12:00:00Z");
const event = {
  canonical_event_id: id, start_at: "2030-06-14T19:30:00-07:00", price_status: "free",
  event_status: "scheduled", registration_status: "open", registration_urls: ["https://lu.ma/event"],
  sources: [],
};
const item = {
  event: { ...event, title: "Example" }, created_at: "2026-10-07T12:00:00Z",
  version: 1, unread: true, status: "queued", outcome: null,
};

test("only upcoming free Luma or Meetup events can be queued", () => {
  assert.equal(museEligibility(event, now), null);
  for (const changes of [
    { price_status: "paid" }, { price_status: "unknown" }, { event_status: "cancelled" },
    { start_at: "invalid" }, { start_at: "2020-01-01T00:00:00Z" }, { registration_status: "sold_out" },
    { registration_urls: ["https://unreviewed.test/event"] },
  ]) assert.ok(museEligibility({ ...event, ...changes }, now));
});

test("provider URLs reject credential links and unapproved origins", () => {
  assert.equal(museProviderUrl("https://www.meetup.com/group/events/123/"), "https://www.meetup.com/group/events/123/");
  for (const url of ["javascript:alert(1)", "https://lu.ma.evil.test/event", "https://lu.ma/",
    "https://user:secret@lu.ma/event", "https://lu.ma:444/event", "https://lu.ma/event#token",
    "https://lu.ma/event\n", "https://lu.ma/\\evil"]) assert.equal(museProviderUrl(url), null);
});

function storage() {
  const entries = new Map();
  return { entries, setItem: (key, value) => entries.set(key, value),
    getItem: key => entries.get(key) ?? null, removeItem: key => entries.delete(key) };
}
test("one explicit signup intent stores only its public ID and is consumed once", () => {
  const store = storage();
  saveMuseIntent(store, id, now);
  assert.deepEqual(JSON.parse([...store.entries.values()][0]), { eventId: id, savedAt: now });
  assert.equal(takeMuseIntent(store, now + 1), id);
  assert.equal(takeMuseIntent(store, now + 2), null);
  saveMuseIntent(store, id, now);
  assert.equal(takeMuseIntent(store, now + 31 * 60 * 1000), null);
  saveMuseIntent(store, id, now);
  assert.equal(takeMuseIntent(store, now - 1), null);
  saveMuseIntent(store, "not-an-id", now);
  assert.equal(store.entries.size, 0);
  store.setItem("ec:muse:intent", JSON.stringify({ eventIds: [id], savedAt: now }));
  assert.equal(takeMuseIntent(store, now), null);
});

test("a delayed list response cannot roll back progress or duplicate an existing task", () => {
  const advanced = { ...item, version: 3, status: "registered" };
  const other = { ...item, event: { ...item.event, canonical_event_id: "33333333-3333-4333-8333-333333333333" }, created_at: "2026-10-08T12:00:00Z" };
  assert.deepEqual(mergeMuseRegistrations([advanced], [item, other]), [other, advanced]);
  assert.deepEqual(mergeMuseRegistrations([item], [advanced]), [advanced]);
});

test("viewed versions do not revive from a stale response or consume a future update", () => {
  const seen = new Map([[id, 2]]);
  assert.equal(museUnreadCount(6, [{ ...item, version: 2 }], seen), 5);
  assert.equal(museUnreadCount(6, [{ ...item, version: 3 }], seen), 6);
  assert.equal(museUnreadCount(0, [item], seen), 0);
});

test("status changes and a new question are announced; old responses stay silent", () => {
  const question = { ...item, status: "needs_input", version: 2, outcome: { note: "Which company?" } };
  assert.match(museRegistrationUpdates([item], [question])[0], /Needs your input.*Which company/);
  const newQuestion = { ...question, version: 3, outcome: { note: "Which job title?" } };
  assert.match(museRegistrationUpdates([question], [newQuestion])[0], /Which job title/);
  assert.deepEqual(museRegistrationUpdates([newQuestion], [question]), []);
  assert.deepEqual(museRegistrationUpdates([], [item]), []);
});

test("handoff covers every page, protects attempts, and distinguishes reported registration", () => {
  const instruction = museInstruction();
  assert.ok(instruction.includes("every page") && instruction.includes("cursor") && instruction.includes("Claim each queued event"));
  assert.ok(instruction.includes("approval") && instruction.includes("uncertain") && instruction.includes("without claiming a new attempt"));
  assert.equal(museStatusLabel.registered, "Registered · reported by Muse");
  assert.notEqual(museStatusLabel.awaiting_approval, museStatusLabel.registered);
});

test("queue, page and seen requests use the owner API, exact IDs and cancellation signal", async () => {
  const original = globalThis.fetch;
  const calls = [];
  const signal = new AbortController().signal;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    return new Response(options.method === "POST" && path.endsWith("/seen") ? null : "{}", { status: path.endsWith("/seen") ? 204 : 200, headers: { "content-type": "application/json" } });
  };
  try {
    await queueMuseRegistration("owner", id, id, signal);
    await getMuseRegistrations("owner", id, signal);
    await markMuseRegistrationsSeen("owner", [{ event_id: id, version: 3 }], signal);
    assert.equal(calls[0].path, "/v1/me/muse/registrations");
    assert.deepEqual(JSON.parse(calls[0].options.body), { request_id: id, event_id: id });
    assert.equal(calls[1].path, `/v1/me/muse/registrations?limit=50&cursor=${id}`);
    assert.deepEqual(JSON.parse(calls[2].options.body), { items: [{ event_id: id, version: 3 }] });
    for (const call of calls) {
      assert.equal(call.options.signal, signal);
      assert.equal(call.options.headers.get("X-EC-Tenant-ID"), "owner");
      assert.equal(call.options.credentials, "same-origin");
    }
  } finally { globalThis.fetch = original; }
});
