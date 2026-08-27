import assert from "node:assert/strict";
import test from "node:test";

import { collectRoster } from "../lib/admin-source-roster.ts";

/**
 * The defect being fixed is request shaping: the old client asked for exactly one page of 100 and
 * presented the result as the whole registry.
 */

const CEILING = 1_000;

function pager(total, pageSize = 100) {
  const calls = [];
  const fetchPage = async (offset) => {
    calls.push(offset);
    const count = Math.max(0, Math.min(pageSize, total - offset));
    return {
      total,
      items: Array.from({ length: count }, (_, i) => ({ source_key: `s-${offset + i}` })),
    };
  };
  return { fetchPage, calls };
}

const identity = (item) => item.source_key;

test("a roster that fits in one page costs exactly one request", async () => {
  const { fetchPage, calls } = pager(72);
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  assert.deepEqual(calls, [0]);
  assert.equal(result.items.length, 72);
  assert.equal(result.truncated, false);
});

test("a roster larger than one page is collected to completion", async () => {
  const { fetchPage, calls } = pager(250);
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  // The old client stopped at 100 of 250 and said nothing about the other 150.
  assert.deepEqual(calls, [0, 100, 200]);
  assert.equal(result.items.length, 250);
  assert.equal(result.truncated, false);
  assert.equal(result.requests, 3);
});

test("an empty page ends collection and the shortfall is reported", async () => {
  const calls = [];
  const fetchPage = async (offset) => {
    calls.push(offset);
    return offset === 0
      ? { total: 999, items: [{ source_key: "a" }] }
      : { total: 999, items: [] };
  };
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  assert.equal(calls.length, 2);
  // The server claimed 999 and delivered 1: that must surface, not be hidden.
  assert.equal(result.truncated, true);
  assert.equal(result.total, 999);
});

test("a page of only duplicates ends collection instead of looping forever", async () => {
  let requests = 0;
  const fetchPage = async () => {
    requests += 1;
    return { total: 500, items: [{ source_key: "same" }] };
  };
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  assert.ok(requests <= 2, `expected collection to stop, made ${requests} requests`);
  assert.equal(result.items.length, 1);
  assert.equal(result.truncated, true);
});

test("the ceiling bounds collection and reports truncation", async () => {
  const { fetchPage } = pager(CEILING + 500);
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  assert.ok(result.items.length <= CEILING + 100);
  assert.equal(result.truncated, true);
});

test("rows repeated across pages are not double counted", async () => {
  let offsetSeen = 0;
  const fetchPage = async (offset) => {
    offsetSeen = offset;
    return {
      total: 4,
      items: [{ source_key: "shared" }, { source_key: `unique-${offset}` }],
    };
  };
  const result = await collectRoster({ fetchPage, identity, ceiling: CEILING });

  const keys = result.items.map(identity);
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(offsetSeen >= 0);
});
