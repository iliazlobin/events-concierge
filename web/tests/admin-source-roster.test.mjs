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

test("duplicate-only pages advance the raw offset and do not hide later rows", async () => {
  const calls = [];
  const pages = [
    ["a", "b", "a"],
    ["a", "b", "a"],
    ["c", "d"],
  ];
  const result = await collectRoster({
    fetchPage: async (offset) => {
      calls.push(offset);
      return { total: 8, items: pages[calls.length - 1].map((source_key) => ({ source_key })) };
    },
    identity,
    ceiling: CEILING,
  });

  assert.deepEqual(calls, [0, 3, 6]);
  assert.deepEqual(result.items.map(identity), ["a", "b", "c", "d"]);
  assert.equal(result.total, 8);
  assert.equal(result.truncated, true);
});

test("the ceiling is strict even when the last page crosses it", async () => {
  const { fetchPage, calls } = pager(500);
  const result = await collectRoster({ fetchPage, identity, ceiling: 125 });

  assert.deepEqual(calls, [0, 100]);
  assert.equal(result.items.length, 125);
  assert.equal(result.items.at(-1).source_key, "s-124");
  assert.equal(result.truncated, true);
});

test("an oversized first page is capped before it is published", async () => {
  const { fetchPage, calls } = pager(500, 500);
  const progress = [];
  const result = await collectRoster({
    fetchPage, identity, ceiling: 25, onProgress: (snapshot) => progress.push(snapshot),
  });

  assert.deepEqual(calls, [0]);
  assert.equal(result.items.length, 25);
  assert.equal(progress[0].items.length, 25);
  assert.equal(result.truncated, true);
});

test("a request budget bounds tiny duplicate pages despite a wrong total", async () => {
  const calls = [];
  const result = await collectRoster({
    fetchPage: async (offset) => {
      calls.push(offset);
      return { total: 100_000, items: [{ source_key: "same" }] };
    },
    identity,
    ceiling: CEILING,
    maxRequests: 3,
  });

  assert.deepEqual(calls, [0, 1, 2]);
  assert.equal(result.requests, 3);
  assert.equal(result.items.length, 1);
  assert.equal(result.truncated, true);
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("the first page is published while the second is pending, with stable snapshots", { timeout: 1_000 }, async () => {
  const secondStarted = deferred();
  const secondPage = deferred();
  const progress = [];
  const resultPromise = collectRoster({
    fetchPage: async (offset) => {
      if (offset === 0) return { total: 3, items: [{ source_key: "a" }, { source_key: "b" }] };
      secondStarted.resolve();
      return secondPage.promise;
    },
    identity,
    ceiling: CEILING,
    onProgress: (snapshot) => progress.push(snapshot),
  });
  await secondStarted.promise;
  assert.equal(progress.length, 1);
  assert.deepEqual(progress[0].items.map(identity), ["a", "b"]);
  assert.equal(progress[0].total, 3);
  assert.equal(progress[0].truncated, true);

  secondPage.resolve({ total: 3, items: [{ source_key: "c" }] });
  const result = await resultPromise;
  assert.deepEqual(result.items.map(identity), ["a", "b", "c"]);
  assert.equal(result.truncated, false);
  assert.deepEqual(progress[0].items.map(identity), ["a", "b"]);
  assert.deepEqual(progress.at(-1).items.map(identity), ["a", "b", "c"]);
});

test("a failed later page preserves the last published partial snapshot and rejects", async () => {
  const unavailable = new Error("Page two unavailable");
  const progress = [];
  await assert.rejects(collectRoster({
    fetchPage: async (offset) => {
      if (offset === 0) return { total: 5, items: [{ source_key: "a" }] };
      throw unavailable;
    },
    identity,
    ceiling: CEILING,
    onProgress: (snapshot) => progress.push(snapshot),
  }), (error) => error === unavailable);

  assert.equal(progress.length, 1);
  assert.deepEqual(progress[0].items.map(identity), ["a"]);
  assert.equal(progress[0].total, 5);
  assert.equal(progress[0].truncated, true);
});

test("a pending noncooperative page is canceled promptly and cannot publish or continue", { timeout: 1_000 }, async () => {
  const controller = new AbortController();
  const secondStarted = deferred();
  const secondPage = deferred();
  const calls = [];
  const progress = [];
  const resultPromise = collectRoster({
    fetchPage: async (offset) => {
      calls.push(offset);
      if (offset === 0) return { total: 3, items: [{ source_key: "a" }] };
      secondStarted.resolve();
      return secondPage.promise;
    },
    identity,
    ceiling: CEILING,
    signal: controller.signal,
    onProgress: (snapshot) => progress.push(snapshot),
  });
  await secondStarted.promise;
  controller.abort();
  await assert.rejects(resultPromise, { name: "AbortError" });
  secondPage.resolve({ total: 3, items: [{ source_key: "late" }] });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls, [0, 1]);
  assert.equal(progress.length, 1);
  assert.deepEqual(progress[0].items.map(identity), ["a"]);
});

test("an already canceled scope makes no request and publishes nothing", async () => {
  const controller = new AbortController();
  controller.abort();
  const { fetchPage, calls } = pager(250);
  const progress = [];
  await assert.rejects(collectRoster({
    fetchPage, identity, ceiling: CEILING, signal: controller.signal,
    onProgress: (snapshot) => progress.push(snapshot),
  }), { name: "AbortError" });

  assert.deepEqual(calls, []);
  assert.deepEqual(progress, []);
});
